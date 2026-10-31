"""
Inventory views — packages, labels, scanning, put-away, transfers, repacking,
movement ledger and stock queries. Rules live in services.py.
"""
import io
from decimal import Decimal, InvalidOperation

import django_filters
import qrcode
from django.db.models import Count, Q
from django.http import HttpResponse
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter
from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.exceptions import BusinessRuleViolation
from apps.core.permissions import role_permission
from apps.receiving.models import ReceiptStatus

from apps.core.idempotency import run_idempotent, key_from_request
from apps.receiving.serializers import ReasonSerializer
from .models import (
    Package, PackageStatus, QCStatus, InventoryMovement, StockAdjustment, MaterialRequest, RequestStatus,
    MaterialIssue, MaterialReturn, DisposalRecord, QC_PENDING_STATUSES,
)
from .serializers import (
    PackageSerializer, PackageListSerializer, InventoryMovementSerializer,
    LabelPrintSerializer, LabelDataSerializer, LocationMoveSerializer, SplitSerializer,
    TimelineEventSerializer, StockAdjustmentSerializer, StockAdjustmentInputSerializer, DecisionNotesSerializer,
    MaterialRequestSerializer, MaterialRequestInputSerializer,
    MaterialIssueSerializer, MaterialIssueInputSerializer,
    MaterialReturnSerializer, MaterialReturnInputSerializer, DisposalRecordSerializer, DisposalInputSerializer,
)
from . import services, issuing

Handlers = role_permission('ADMIN', 'RECEIVING', 'WAREHOUSE')
WarehouseOps = role_permission('ADMIN', 'WAREHOUSE')
TagPrinters = role_permission('ADMIN', 'WAREHOUSE', 'LAB_REVIEWER')


class PackageFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=PackageStatus.choices)
    qc_status = django_filters.MultipleChoiceFilter(choices=QCStatus.choices)
    receipt = django_filters.UUIDFilter(field_name='invoice_line__receipt')
    warehouse = django_filters.NumberFilter(field_name='current_location__warehouse')
    batch_number = django_filters.CharFilter(lookup_expr='iexact')
    available = django_filters.BooleanFilter(method='filter_available')
    in_warehouse = django_filters.BooleanFilter(method='filter_in_warehouse')
    expiring_before = django_filters.DateFilter(field_name='expiry_date', lookup_expr='lte')

    class Meta:
        model = Package
        fields = ['status', 'qc_status', 'material', 'supplier', 'current_location', 'invoice_line',
                  'is_on_hold', 'is_recalled', 'parent_package']

    def filter_available(self, qs, name, value):
        available = Package.objects.available().values('pk')
        return qs.filter(pk__in=available) if value else qs.exclude(pk__in=available)

    def filter_in_warehouse(self, qs, name, value):
        ids = Package.objects.in_warehouse().values('pk')
        return qs.filter(pk__in=ids) if value else qs.exclude(pk__in=ids)


@extend_schema_view(list=extend_schema(tags=['inventory']), retrieve=extend_schema(tags=['inventory']))
class PackageViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Packages are created only from verified invoice lines
    (POST /receiving/invoice-lines/{id}/packages/) and change only through the actions below.
    """
    filterset_class = PackageFilter
    search_fields = ['package_id', 'batch_number', 'material__code', 'material__name',
                     'invoice_line__receipt__receipt_number', 'supplier__name']
    ordering_fields = ['created_at', 'received_at', 'expiry_date', 'current_quantity', 'package_id']
    ordering = ['-created_at']

    def get_queryset(self):
        return Package.objects.select_related(
            'material', 'unit', 'current_location__warehouse', 'supplier', 'invoice_line__receipt', 'parent_package',
        ).prefetch_related('label_events__printed_by')

    def get_serializer_class(self):
        return PackageListSerializer if self.action == 'list' else PackageSerializer

    def _detail(self, pkg):
        return PackageSerializer(self.get_queryset().get(pk=pkg.pk), context=self.get_serializer_context()).data

    @extend_schema(tags=['inventory'], parameters=[OpenApiParameter('code', str, required=True,
                   description='QR content, opaque token, or printed package ID')], responses=PackageSerializer)
    @action(detail=False, methods=['get'], url_path='resolve')
    def resolve(self, request):
        """Scan-to-open: resolve scanner/camera input to the current server-side package record."""
        pkg = services.resolve_scan(request.query_params.get('code'), request.user, request=request)
        return Response(self._detail(pkg))

    @extend_schema(tags=['inventory'], responses=LabelDataSerializer)
    @action(detail=True, methods=['get'], url_path='label')
    def label(self, request, pk=None):
        """Data for rendering a label. Contains no mutable status."""
        return Response(services.label_data(self.get_object()))

    @extend_schema(tags=['inventory'], responses={(200, 'image/png'): OpenApiTypes.BINARY})
    @action(detail=True, methods=['get'], url_path='qr')
    def qr_image(self, request, pk=None):
        pkg = self.get_object()
        buf = io.BytesIO()
        qrcode.make(services.qr_payload(pkg), box_size=8, border=2).save(buf, format='PNG')
        return HttpResponse(buf.getvalue(), content_type='image/png')

    @extend_schema(tags=['inventory'], request=LabelPrintSerializer, responses=PackageSerializer)
    @action(detail=True, methods=['post'], url_path='print-label', permission_classes=[Handlers])
    def print_label(self, request, pk=None):
        """Record a label print. The first print releases the package for put-away; reprints need a reason."""
        s = LabelPrintSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pkg, _ = services.record_label_print(
            self.get_object(), request.user, printer_reference=s.validated_data['printer_reference'],
            reason=s.validated_data['reason'], request=request)
        return Response(self._detail(pkg))

    @extend_schema(tags=['inventory'], request=LabelPrintSerializer, responses=PackageSerializer)
    @action(detail=True, methods=['post'], url_path='print-release-tag', permission_classes=[TagPrinters])
    def print_release_tag(self, request, pk=None):
        """Green release tag — refused unless the server confirms the package is released."""
        s = LabelPrintSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pkg, _ = services.record_release_tag(
            self.get_object(), request.user, reason=s.validated_data['reason'],
            printer_reference=s.validated_data['printer_reference'], request=request)
        return Response(self._detail(pkg))

    @extend_schema(tags=['inventory'], request=LocationMoveSerializer, responses=PackageSerializer)
    @action(detail=True, methods=['post'], url_path='putaway', permission_classes=[Handlers])
    def putaway(self, request, pk=None):
        s = LocationMoveSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pkg = services.putaway_package(self.get_object(), s.validated_data['location'], request.user,
                                       notes=s.validated_data['notes'], request=request)
        return Response(self._detail(pkg))

    @extend_schema(tags=['inventory'], request=LocationMoveSerializer, responses=PackageSerializer)
    @action(detail=True, methods=['post'], url_path='transfer', permission_classes=[WarehouseOps])
    def transfer(self, request, pk=None):
        s = LocationMoveSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        pkg = services.transfer_package(self.get_object(), s.validated_data['location'], request.user,
                                        notes=s.validated_data['notes'], request=request)
        return Response(self._detail(pkg))

    @extend_schema(tags=['inventory'], request=SplitSerializer, responses=PackageListSerializer(many=True))
    @action(detail=True, methods=['post'], url_path='split', permission_classes=[WarehouseOps])
    def split(self, request, pk=None):
        """Repack into child packages with new IDs; returns the children (labels must be printed)."""
        s = SplitSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        _, children = services.split_package(self.get_object(), s.validated_data['quantities'], request.user,
                                             reason=s.validated_data['reason'], request=request)
        return Response(PackageListSerializer(children, many=True).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['inventory'], responses=InventoryMovementSerializer(many=True))
    @action(detail=True, methods=['get'], url_path='movements')
    def movements(self, request, pk=None):
        qs = self.get_object().movements.select_related(
            'from_location__warehouse', 'to_location__warehouse', 'performed_by', 'unit', 'package')
        return Response(InventoryMovementSerializer(qs.order_by('created_at'), many=True).data)

    @extend_schema(tags=['inventory'], responses=TimelineEventSerializer(many=True))
    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        """Full lifecycle from gate receipt to current state."""
        return Response(TimelineEventSerializer(services.package_timeline(self.get_object()), many=True).data)


@extend_schema_view(list=extend_schema(tags=['inventory']), retrieve=extend_schema(tags=['inventory']))
class InventoryMovementViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = InventoryMovement.objects.select_related(
        'package', 'from_location__warehouse', 'to_location__warehouse', 'performed_by', 'unit')
    serializer_class = InventoryMovementSerializer
    filterset_fields = ['package', 'reason', 'from_location', 'to_location', 'performed_by']
    search_fields = ['package__package_id', 'reference_id']
    ordering = ['-created_at']


def _stock_queryset(request):
    qs = Package.objects.all()
    params = request.query_params
    for param, field in (('material', 'material_id'), ('location', 'current_location_id'),
                         ('warehouse', 'current_location__warehouse_id'), ('supplier', 'supplier_id'),
                         ('qc_status', 'qc_status'), ('batch_number', 'batch_number__iexact')):
        if params.get(param):
            qs = qs.filter(**{field: params[param]})
    return qs


class StockViewSet(viewsets.ViewSet):
    """Stock balances, ageing, expiry, reconciliation and the warehouse dashboard — all from persisted state."""
    permission_classes = [permissions.IsAuthenticated]

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('group_by', str, description=f'Comma list of: {", ".join(services.GROUP_FIELDS)}'),
        OpenApiParameter('material', int), OpenApiParameter('location', int), OpenApiParameter('warehouse', int),
        OpenApiParameter('qc_status', str), OpenApiParameter('batch_number', str)])
    @action(detail=False, methods=['get'])
    def balances(self, request):
        group_by = [g for g in (request.query_params.get('group_by') or 'material').split(',') if g]
        unknown = set(group_by) - set(services.GROUP_FIELDS)
        if unknown:
            raise BusinessRuleViolation(f'Unknown group_by: {", ".join(sorted(unknown))}.')
        return Response({'group_by': group_by, 'results': services.stock_balances(_stock_queryset(request), group_by)})

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['get'])
    def ageing(self, request):
        return Response(services.stock_ageing(_stock_queryset(request)))

    @extend_schema(tags=['inventory'], parameters=[OpenApiParameter('days', int)],
                   responses=PackageListSerializer(many=True))
    @action(detail=False, methods=['get'])
    def expiry(self, request):
        qs = services.expiry_exposure(_stock_queryset(request), request.query_params.get('days')).select_related(
            'material', 'unit', 'current_location', 'invoice_line__receipt')
        return Response(PackageListSerializer(qs[:500], many=True).data)

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['get'], url_path='low-stock')
    def low_stock(self, request):
        return Response(services.low_stock_materials())

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['get'])
    def reconciliation(self, request):
        issues = services.reconciliation_issues()
        return Response({'reconciled': not issues, 'issues': issues})

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT, parameters=[
        OpenApiParameter('material_id', int, required=True), OpenApiParameter('quantity', str, required=True),
        OpenApiParameter('unit_id', int, description="Defaults to the material's default unit")])
    @action(detail=False, methods=['get'], url_path='fifo-suggestion')
    def fifo_suggestion(self, request):
        """Eligible, unreserved released stock in FIFO (received date) or FEFO (expiry / retest) order."""
        from apps.master.models import Material, UnitOfMeasure
        params = request.query_params
        try:
            qty = Decimal(params.get('quantity', ''))
            material = Material.objects.get(pk=int(params.get('material_id', '')))
        except (InvalidOperation, ValueError, Material.DoesNotExist):
            raise BusinessRuleViolation('A valid material_id and numeric quantity are required.')
        unit = UnitOfMeasure.objects.filter(pk=params.get('unit_id')).first() if params.get('unit_id') else material.default_unit
        if unit is None:
            raise BusinessRuleViolation('unit_id is required for this material.')
        return Response(issuing.suggest(material, unit, qty))

    @extend_schema(tags=['inventory'], responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """Warehouse stock dashboard counts."""
        in_wh = Package.objects.in_warehouse()
        available = Package.objects.available()
        near_days = services.get_config('stock.near_expiry_days')
        return Response({
            'packages_in_warehouse': in_wh.count(),
            'by_status': dict(in_wh.values_list('status').annotate(c=Count('id'))),
            'by_qc_status': dict(in_wh.values_list('qc_status').annotate(c=Count('id'))),
            'available_packages': available.count(),
            'awaiting_label': in_wh.filter(status=PackageStatus.CREATED).count(),
            'awaiting_putaway': in_wh.filter(status=PackageStatus.AWAITING_PUTAWAY).count(),
            'in_quarantine_location': in_wh.filter(current_location__is_quarantine=True).count(),
            'pending_qc': in_wh.filter(qc_status__in=QC_PENDING_STATUSES).count(),
            'on_hold': in_wh.filter(Q(is_on_hold=True) | Q(qc_status=QCStatus.ON_HOLD)).count(),
            'rejected': in_wh.filter(qc_status=QCStatus.FAILED).count(),
            'recalled': in_wh.filter(is_recalled=True).count(),
            'near_expiry_or_retest': services.expiry_exposure(Package.objects.all()).count(),
            'near_expiry_days': near_days,
            'low_stock': services.low_stock_materials(),
            'receipts_awaiting_putaway': Package.objects.filter(
                status__in=[PackageStatus.CREATED, PackageStatus.AWAITING_PUTAWAY],
                invoice_line__receipt__status__in=[ReceiptStatus.UNLOADED, ReceiptStatus.PUTAWAY_IN_PROGRESS],
            ).values('invoice_line__receipt').distinct().count(),
        })


# ---------------------------------------------------------------------------
# Phase 6 — requests, allocation, issue, returns, disposals, adjustments.
# Rules live in issuing.py.
# ---------------------------------------------------------------------------

def _factory_scoped(user, qs, *owner_paths):
    """Object-level scoping: factory requesters only see records they raised or received."""
    if user.has_role('FACTORY') and not user.is_superuser:
        cond = Q()
        for path in owner_paths:
            cond |= Q(**{path: user})
        return qs.filter(cond).distinct()
    return qs


class RequestFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=RequestStatus.choices)
    material = django_filters.NumberFilter(field_name='lines__material', distinct=True)

    class Meta:
        model = MaterialRequest
        fields = ['status', 'factory', 'department', 'material', 'production_order']


@extend_schema_view(list=extend_schema(tags=['issue']), retrieve=extend_schema(tags=['issue']),
                    create=extend_schema(tags=['issue'], request=MaterialRequestInputSerializer,
                                         responses=MaterialRequestSerializer))
class MaterialRequestViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    serializer_class = MaterialRequestSerializer
    filterset_class = RequestFilter
    search_fields = ['request_number', 'production_order', 'cost_centre', 'factory__name', 'lines__material__code']
    ordering_fields = ['created_at', 'required_by']
    ordering = ['-created_at']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):  # schema generation has no user
            return MaterialRequest.objects.none()
        qs = MaterialRequest.objects.select_related('factory', 'department', 'requested_by').prefetch_related(
            'lines__material', 'lines__unit', 'issues')
        return _factory_scoped(self.request.user, qs, 'requested_by')

    def _detail(self, req, code=status.HTTP_200_OK):
        return Response(MaterialRequestSerializer(self.get_queryset().get(pk=req.pk)).data, status=code)

    def create(self, request, *args, **kwargs):
        s = MaterialRequestInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(issuing.create_request(s.validated_data, request.user, request=request), status.HTTP_201_CREATED)

    @extend_schema(tags=['issue'], responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['get'])
    def suggestions(self, request, pk=None):
        """FIFO / FEFO pick list for each open line (eligible, unreserved stock only)."""
        req = self.get_object()
        return Response([{'line': str(l.pk), 'material_code': l.material.code, 'remaining': str(l.remaining_quantity),
                          'unit': l.unit.symbol, **issuing.suggest(l.material, l.unit, l.remaining_quantity, request_line=l)}
                         for l in req.lines.select_related('material', 'unit') if l.remaining_quantity > 0])

    @extend_schema(tags=['issue'], request=None, responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['post'])
    def allocate(self, request, pk=None):
        """Reserve stock for the request in FIFO / FEFO order."""
        req, summary = issuing.allocate_request(self.get_object(), request.user, request=request)
        return Response({'request': MaterialRequestSerializer(self.get_queryset().get(pk=req.pk)).data, 'allocation': summary})

    @extend_schema(tags=['issue'], request=ReasonSerializer, responses=MaterialRequestSerializer)
    @action(detail=True, methods=['post'], url_path='release-allocation')
    def release_allocation(self, request, pk=None):
        s = ReasonSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(issuing.release_allocation(self.get_object(), request.user, reason=s.validated_data['reason'],
                                                       request=request))

    @extend_schema(tags=['issue'], request=ReasonSerializer, responses=MaterialRequestSerializer)
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        s = ReasonSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(issuing.cancel_request(self.get_object(), request.user, reason=s.validated_data['reason'],
                                                   request=request))

    @extend_schema(tags=['issue'], request=ReasonSerializer, responses=MaterialRequestSerializer)
    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        s = ReasonSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(issuing.close_request(self.get_object(), request.user, reason=s.validated_data['reason'],
                                                  request=request))


class IssueFilter(django_filters.FilterSet):
    issued_from = django_filters.DateFilter(field_name='issue_datetime', lookup_expr='date__gte')
    issued_to = django_filters.DateFilter(field_name='issue_datetime', lookup_expr='date__lte')
    material = django_filters.NumberFilter(field_name='issue_lines__material', distinct=True)
    package = django_filters.UUIDFilter(field_name='issue_lines__package', distinct=True)

    class Meta:
        model = MaterialIssue
        fields = ['status', 'factory', 'department', 'material_request', 'fifo_override', 'material', 'package',
                  'production_order']


@extend_schema_view(list=extend_schema(tags=['issue']), retrieve=extend_schema(tags=['issue']),
                    create=extend_schema(tags=['issue'], request=MaterialIssueInputSerializer, responses=MaterialIssueSerializer,
                                         parameters=[OpenApiParameter('Idempotency-Key', str, OpenApiParameter.HEADER)]))
class MaterialIssueViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    serializer_class = MaterialIssueSerializer
    filterset_class = IssueFilter
    search_fields = ['issue_number', 'production_order', 'reference', 'material_request__request_number',
                     'issue_lines__package__package_id', 'issue_lines__batch_number']
    ordering_fields = ['issue_datetime']
    ordering = ['-issue_datetime']

    def get_queryset(self):
        if getattr(self, 'swagger_fake_view', False):  # schema generation has no user
            return MaterialIssue.objects.none()
        qs = MaterialIssue.objects.select_related(
            'factory', 'department', 'issued_by', 'received_by', 'acknowledged_by', 'material_request',
            'fifo_override_approved_by').prefetch_related('issue_lines__package', 'issue_lines__material',
                                                          'issue_lines__unit', 'issue_lines__returns')
        return _factory_scoped(self.request.user, qs, 'material_request__requested_by', 'received_by')

    def create(self, request, *args, **kwargs):
        """Issue packages in one atomic transaction (retry-safe with an Idempotency-Key header)."""
        s = MaterialIssueInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = dict(s.validated_data)
        override = data.pop('override_reason', '')

        def work():
            issue = issuing.create_issue(data, request.user, override_reason=override, request=request)
            return MaterialIssueSerializer(self.get_queryset().get(pk=issue.pk)).data

        body, replayed = run_idempotent(request.user, key_from_request(request), 'create_issue', request.data, work)
        return Response(body, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
                        headers={'Idempotent-Replayed': 'true'} if replayed else None)

    @extend_schema(tags=['issue'], request=None, responses=MaterialIssueSerializer)
    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """Factory acknowledges receipt of the issued material."""
        issue = issuing.acknowledge_issue(self.get_object(), request.user, request=request)
        return Response(MaterialIssueSerializer(self.get_queryset().get(pk=issue.pk)).data)


@extend_schema_view(list=extend_schema(tags=['issue']), retrieve=extend_schema(tags=['issue']),
                    create=extend_schema(tags=['issue'], request=MaterialReturnInputSerializer, responses=MaterialReturnSerializer))
class MaterialReturnViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    queryset = MaterialReturn.objects.select_related('issue_line__issue', 'issue_line__package', 'return_package', 'unit')
    serializer_class = MaterialReturnSerializer
    filterset_fields = ['issue_line__issue', 'requires_qc']
    search_fields = ['return_number', 'issue_line__issue__issue_number', 'return_package__package_id']
    ordering = ['-created_at']

    def create(self, request, *args, **kwargs):
        s = MaterialReturnInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        ret = issuing.create_return(s.validated_data, request.user, request=request)
        return Response(MaterialReturnSerializer(self.get_queryset().get(pk=ret.pk)).data, status=status.HTTP_201_CREATED)


@extend_schema_view(list=extend_schema(tags=['inventory']), retrieve=extend_schema(tags=['inventory']),
                    create=extend_schema(tags=['inventory'], request=DisposalInputSerializer, responses=DisposalRecordSerializer))
class DisposalRecordViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    queryset = DisposalRecord.objects.select_related('package', 'unit', 'authorized_by')
    serializer_class = DisposalRecordSerializer
    filterset_fields = ['disposal_type', 'package']
    search_fields = ['disposal_number', 'package__package_id', 'reference']
    ordering = ['-created_at']

    def create(self, request, *args, **kwargs):
        s = DisposalInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        rec = issuing.create_disposal(s.validated_data, request.user, request=request)
        return Response(DisposalRecordSerializer(rec).data, status=status.HTTP_201_CREATED)


@extend_schema_view(list=extend_schema(tags=['inventory']), retrieve=extend_schema(tags=['inventory']),
                    create=extend_schema(tags=['inventory'], request=StockAdjustmentInputSerializer,
                                         responses=StockAdjustmentSerializer))
class StockAdjustmentViewSet(viewsets.ModelViewSet):
    """INCREASE / DECREASE carry a positive amount; CORRECTION carries the physically counted quantity."""
    http_method_names = ['get', 'post', 'head', 'options']
    queryset = StockAdjustment.objects.select_related('package', 'unit', 'created_by', 'approved_by')
    serializer_class = StockAdjustmentSerializer
    filterset_fields = ['status', 'adjustment_type', 'package']
    ordering = ['-created_at']

    def create(self, request, *args, **kwargs):
        s = StockAdjustmentInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        adj = issuing.request_adjustment(s.validated_data, request.user, request=request)
        return Response(StockAdjustmentSerializer(adj).data, status=status.HTTP_201_CREATED)

    def _decide(self, request, approve):
        s = DecisionNotesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        adj = issuing.decide_adjustment(self.get_object(), request.user, approve=approve,
                                        notes=s.validated_data['notes'], request=request)
        return Response(StockAdjustmentSerializer(adj).data)

    @extend_schema(tags=['inventory'], request=DecisionNotesSerializer, responses=StockAdjustmentSerializer)
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        return self._decide(request, True)

    @extend_schema(tags=['inventory'], request=DecisionNotesSerializer, responses=StockAdjustmentSerializer)
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        return self._decide(request, False)
