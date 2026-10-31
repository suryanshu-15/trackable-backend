"""
Receiving views — GoodsReceipt, InvoiceLine, Document, Verification.
Views translate HTTP to service calls; rules live in services.py.
"""
from datetime import timedelta

import django_filters
from django.db.models import Count, Exists, OuterRef
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

from .models import (
    GoodsReceipt, InvoiceLine, ReceiptDocument,
    ReceiptLineVerification, ReceiptStatus, VerificationStatus,
)
from .serializers import (
    GoodsReceiptSerializer, GoodsReceiptListSerializer,
    InvoiceLineSerializer, ReceiptDocumentSerializer, ReceiptDocumentUploadSerializer,
    ReceiptLineVerificationSerializer, WeightCorrectionSerializer,
    TransitionSerializer, AuditEventSerializer, VerificationInputSerializer, ReasonSerializer,
)
from apps.core.idempotency import run_idempotent, key_from_request
from apps.inventory import services as inventory_services
from apps.inventory.models import Package
from apps.inventory.serializers import PackageCreateRequestSerializer, PackageListSerializer
from . import services
from apps.core.audit import record_audit
from apps.core.models import AuditEvent
from apps.core.permissions import role_permission

# Invoices and supplier documents are restricted; factory requesters do not see them.
ReceiptReaders = role_permission(
    'ADMIN', 'GATE', 'RECEIVING', 'WAREHOUSE', 'LAB_TECH', 'LAB_REVIEWER', 'AUDITOR', 'MANAGER',
)
GateWriters = role_permission('ADMIN', 'GATE')
LineWriters = role_permission('ADMIN', 'GATE', 'RECEIVING')
DocumentWriters = role_permission('ADMIN', 'GATE', 'RECEIVING')
WeightCorrectors = role_permission('ADMIN', 'GATE')
Unloaders = role_permission('ADMIN', 'RECEIVING', 'WAREHOUSE')

ACK_PARAM = OpenApiParameter(
    'acknowledge_duplicate', bool, description='Proceed even though a matching supplier invoice exists.',
)


def _truthy(value):
    return str(value).lower() in ('1', 'true', 'yes')


class GoodsReceiptFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=ReceiptStatus.choices)
    arrival_from = django_filters.DateFilter(field_name='arrival_datetime', lookup_expr='date__gte')
    arrival_to = django_filters.DateFilter(field_name='arrival_datetime', lookup_expr='date__lte')
    invoice_date = django_filters.DateFilter()
    material = django_filters.NumberFilter(field_name='invoice_lines__material', distinct=True)
    vehicle_number = django_filters.CharFilter(lookup_expr='icontains')

    class Meta:
        model = GoodsReceipt
        fields = ['status', 'supplier', 'invoice_date', 'material', 'vehicle_number']


@extend_schema_view(
    list=extend_schema(tags=['receiving']),
    create=extend_schema(tags=['receiving'], parameters=[ACK_PARAM]),
    retrieve=extend_schema(tags=['receiving']),
    update=extend_schema(tags=['receiving'], parameters=[ACK_PARAM]),
    partial_update=extend_schema(tags=['receiving'], parameters=[ACK_PARAM]),
)
class GoodsReceiptViewSet(viewsets.ModelViewSet):
    """
    Gate receipt CRUD + state-machine transition actions.
    Receipts are never deleted; a draft or registered receipt can be cancelled.
    """
    http_method_names = ['get', 'post', 'put', 'patch', 'head', 'options']
    filterset_class = GoodsReceiptFilter
    search_fields = [
        'receipt_number', 'invoice_number', 'vehicle_number', 'supplier__name', 'supplier__code',
        'purchase_order_number', 'delivery_challan_number',
        'invoice_lines__material_name_declared', 'invoice_lines__material__code', 'invoice_lines__batch_number',
    ]
    ordering_fields = ['arrival_datetime', 'created_at', 'receipt_number', 'invoice_date']
    ordering = ['-created_at']

    def get_queryset(self):
        qs = GoodsReceipt.objects.select_related('supplier', 'weight_unit', 'created_by')
        if self.action == 'list':
            return qs.annotate(line_count=Count('invoice_lines', distinct=True))
        return qs.prefetch_related('invoice_lines__material', 'invoice_lines__declared_unit', 'documents__uploaded_by')

    def get_serializer_class(self):
        if self.action == 'list':
            return GoodsReceiptListSerializer
        return GoodsReceiptSerializer

    # Actions that declare their own permission_classes on @action
    ACTION_SCOPED_PERMISSIONS = ('correct_weights', 'upload_document')

    def get_permissions(self):
        if self.action in self.ACTION_SCOPED_PERMISSIONS:
            return super().get_permissions()
        if self.action in ('create', 'update', 'partial_update'):
            return [GateWriters()]
        return [ReceiptReaders()]

    def _ack(self, request):
        return _truthy(request.query_params.get('acknowledge_duplicate') or request.data.get('acknowledge_duplicate'))

    def _detail(self, receipt):
        receipt = self.get_queryset().get(pk=receipt.pk)
        return GoodsReceiptSerializer(receipt, context=self.get_serializer_context()).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = services.create_receipt(
            serializer.validated_data, request.user,
            acknowledge_duplicate=self._ack(request), request=request,
        )
        return Response(self._detail(receipt), status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        receipt = services.update_draft_receipt(
            instance, serializer.validated_data, request.user,
            acknowledge_duplicate=self._ack(request), request=request,
        )
        return Response(self._detail(receipt))

    @extend_schema(tags=['receiving'], request=WeightCorrectionSerializer, responses=GoodsReceiptSerializer)
    @action(detail=True, methods=['post'], url_path='correct-weights', permission_classes=[WeightCorrectors])
    def correct_weights(self, request, pk=None):
        """Authorized correction of weighing data after the draft stage (reason required, audited)."""
        receipt = self.get_object()
        serializer = WeightCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        reason = data.pop('reason')
        receipt = services.correct_weights(receipt, data, request.user, reason=reason, request=request)
        return Response(self._detail(receipt))

    def _transition(self, request, name):
        receipt = self.get_object()
        serializer = TransitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        receipt = services.transition_receipt(
            receipt, name, request.user, reason=serializer.validated_data['reason'], request=request,
        )
        return Response(self._detail(receipt))

    _transition_schema = extend_schema(tags=['receiving'], request=TransitionSerializer, responses=GoodsReceiptSerializer)

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='register')
    def register_at_gate(self, request, pk=None):
        return self._transition(request, 'register_at_gate')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='submit-unloading')
    def submit_for_unloading(self, request, pk=None):
        return self._transition(request, 'submit_for_unloading')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='start-unloading')
    def start_unloading(self, request, pk=None):
        return self._transition(request, 'start_unloading')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='verification-pending')
    def mark_verification_pending(self, request, pk=None):
        return self._transition(request, 'mark_verification_pending')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='flag-exception')
    def flag_exception(self, request, pk=None):
        return self._transition(request, 'flag_exception')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='mark-unloaded')
    def mark_unloaded(self, request, pk=None):
        return self._transition(request, 'mark_unloaded')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='start-putaway')
    def start_putaway(self, request, pk=None):
        return self._transition(request, 'start_putaway')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='mark-stored')
    def mark_stored(self, request, pk=None):
        return self._transition(request, 'mark_stored')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='close')
    def close(self, request, pk=None):
        return self._transition(request, 'close')

    @_transition_schema
    @action(detail=True, methods=['post'], url_path='cancel')
    def cancel(self, request, pk=None):
        return self._transition(request, 'cancel')

    @extend_schema(tags=['receiving'], responses=AuditEventSerializer(many=True))
    @action(detail=True, methods=['get'], url_path='timeline')
    def timeline(self, request, pk=None):
        """Chronological audit trail of the receipt, its lines and documents."""
        receipt = self.get_object()
        events = services.receipt_timeline(receipt)
        return Response(AuditEventSerializer(events, many=True).data)

    @extend_schema(
        tags=['receiving'], request=ReceiptDocumentUploadSerializer, responses=ReceiptDocumentSerializer,
    )
    @action(
        detail=True, methods=['post'], url_path='documents',
        permission_classes=[DocumentWriters], parser_classes=[MultiPartParser, FormParser],
    )
    def upload_document(self, request, pk=None):
        receipt = self.get_object()
        serializer = ReceiptDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        doc = services.attach_document(
            receipt, serializer.validated_data['file'], serializer.validated_data['document_type'],
            request.user, notes=serializer.validated_data['notes'], request=request,
        )
        return Response(ReceiptDocumentSerializer(doc).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['receiving'])
    @action(detail=False, methods=['get'], url_path='dashboard')
    def dashboard(self, request):
        """Gate / inward dashboard counts, computed from persisted state."""
        now = timezone.localtime()
        today = now.date()
        week_start = today - timedelta(days=today.weekday())
        qs = GoodsReceipt.objects.all()
        by_status = dict(qs.values_list('status').annotate(c=Count('id')))
        open_qs = qs.exclude(status__in=[ReceiptStatus.CLOSED, ReceiptStatus.CANCELLED])

        discrepant_lines = ReceiptLineVerification.objects.filter(
            invoice_line__receipt=OuterRef('pk'), verification_status=VerificationStatus.DISCREPANT,
        )
        weight_alerts = [
            r.receipt_number for r in open_qs.filter(net_weight__isnull=False).prefetch_related('invoice_lines')
            if (d := services.weight_discrepancy(r)) and d['exceeds_tolerance']
        ]
        return Response({
            'today_receipts': qs.filter(arrival_datetime__date=today).count(),
            'week_receipts': qs.filter(arrival_datetime__date__gte=week_start).count(),
            'by_status': {s: by_status.get(s, 0) for s in ReceiptStatus.values},
            'trucks_on_site': open_qs.filter(status__in=[
                ReceiptStatus.GATE_REGISTERED, ReceiptStatus.AWAITING_UNLOADING, ReceiptStatus.UNLOADING_IN_PROGRESS,
            ]).count(),
            'awaiting_line_entry': open_qs.filter(
                status__in=[ReceiptStatus.DRAFT, ReceiptStatus.GATE_REGISTERED],
            ).filter(invoice_lines__isnull=True).count(),
            'unloading': {
                'pending': by_status.get(ReceiptStatus.AWAITING_UNLOADING, 0),
                'in_progress': by_status.get(ReceiptStatus.UNLOADING_IN_PROGRESS, 0)
                + by_status.get(ReceiptStatus.VERIFICATION_PENDING, 0),
                'completed': qs.filter(unloading_completed_at__date=today).count(),
            },
            'exceptions': by_status.get(ReceiptStatus.EXCEPTION, 0),
            'quantity_discrepancy_receipts': open_qs.filter(Exists(discrepant_lines)).count(),
            'weight_discrepancy_receipts': weight_alerts[:50],
        })


@extend_schema_view(
    list=extend_schema(tags=['receiving']),
    create=extend_schema(tags=['receiving']),
    retrieve=extend_schema(tags=['receiving']),
    update=extend_schema(tags=['receiving']),
    partial_update=extend_schema(tags=['receiving']),
    destroy=extend_schema(tags=['receiving']),
)
class InvoiceLineViewSet(viewsets.ModelViewSet):
    queryset = InvoiceLine.objects.select_related(
        'material', 'declared_unit', 'receipt', 'verification').prefetch_related('required_tests')
    serializer_class = InvoiceLineSerializer
    filterset_fields = ['receipt', 'material']
    search_fields = ['material_name_declared', 'batch_number', 'material_code_declared']

    def get_permissions(self):
        if self.action in ('verify', 'create_packages'):
            return [Unloaders()]
        if self.action in ('create', 'update', 'partial_update', 'destroy'):
            return [LineWriters()]
        return [ReceiptReaders()]

    @extend_schema(tags=['receiving'], request=VerificationInputSerializer, responses=ReceiptLineVerificationSerializer)
    @action(detail=True, methods=['post'], url_path='verify')
    def verify(self, request, pk=None):
        """Record what was physically received for this line. Discrepancies are computed server-side."""
        s = VerificationInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = services.record_verification(self.get_object(), s.validated_data, request.user, request=request)
        return Response(ReceiptLineVerificationSerializer(v).data)

    @extend_schema(
        tags=['receiving'], request=PackageCreateRequestSerializer, responses=PackageListSerializer(many=True),
        parameters=[OpenApiParameter('Idempotency-Key', str, OpenApiParameter.HEADER,
                                     description='Retry-safe: a repeated key returns the original packages.')],
    )
    @action(detail=True, methods=['post'], url_path='packages')
    def create_packages(self, request, pk=None):
        """Generate uniquely identified packages for the accepted quantity of a verified line."""
        line = self.get_object()
        s = PackageCreateRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        specs = s.validated_data['specs']

        def work():
            packages = inventory_services.create_packages(line, specs, request.user, request=request)
            return PackageListSerializer(
                Package.objects.filter(pk__in=[p.pk for p in packages]).select_related(
                    'material', 'unit', 'current_location', 'invoice_line__receipt').order_by('created_at', 'package_id'),
                many=True).data

        data, replayed = run_idempotent(
            request.user, key_from_request(request), f'create_packages:{line.pk}', request.data, work)
        return Response(data, status=status.HTTP_200_OK if replayed else status.HTTP_201_CREATED,
                        headers={'Idempotent-Replayed': 'true'} if replayed else None)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        receipt = data.pop('receipt')
        line = services.add_invoice_line(receipt, data, request.user, request=request)
        return Response(InvoiceLineSerializer(line).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        line = services.update_invoice_line(instance, dict(serializer.validated_data), request.user, request=request)
        return Response(InvoiceLineSerializer(line).data)

    def destroy(self, request, *args, **kwargs):
        services.delete_invoice_line(self.get_object(), request.user, request=request)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=['receiving'], responses={(200, 'application/octet-stream'): OpenApiTypes.BINARY})
class ReceiptDocumentDownloadView(generics.GenericAPIView):
    """Authenticated, role-checked, audited file download. Media is not served publicly."""
    permission_classes = [ReceiptReaders]
    queryset = ReceiptDocument.objects.select_related('receipt')
    serializer_class = ReceiptDocumentSerializer  # metadata shape; the response body is the file

    def get(self, request, pk):
        doc = get_object_or_404(self.get_queryset(), pk=pk)
        record_audit(
            request.user, AuditEvent.ACTION_DOWNLOAD, 'ReceiptDocument', doc.id,
            extra={'receipt': str(doc.receipt_id)}, request=request,
        )
        response = FileResponse(
            doc.file.open('rb'), as_attachment=True, filename=doc.original_filename,
            content_type=doc.mime_type or 'application/octet-stream',
        )
        response['X-Content-Type-Options'] = 'nosniff'
        return response


@extend_schema_view(list=extend_schema(tags=['receiving']), retrieve=extend_schema(tags=['receiving']))
class ReceiptLineVerificationViewSet(viewsets.ReadOnlyModelViewSet):
    """Verifications are recorded via POST /invoice-lines/{id}/verify/; this exposes them and the review action."""
    queryset = ReceiptLineVerification.objects.select_related(
        'invoice_line__receipt', 'received_unit', 'verified_by', 'exception_authorized_by')
    serializer_class = ReceiptLineVerificationSerializer
    filterset_fields = {
        'verification_status': ['exact', 'in'], 'discrepancy_status': ['exact'],
        'invoice_line__receipt': ['exact'],
    }
    permission_classes = [ReceiptReaders]

    @extend_schema(tags=['receiving'], request=ReasonSerializer, responses=ReceiptLineVerificationSerializer)
    @action(detail=True, methods=['post'], url_path='accept-discrepancy')
    def accept_discrepancy(self, request, pk=None):
        """Authorized acceptance of a discrepant line (exception path). Separation of duties applies."""
        s = ReasonSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        v = services.accept_discrepancy(self.get_object(), request.user, reason=s.validated_data['reason'],
                                        request=request)
        return Response(ReceiptLineVerificationSerializer(v).data)
