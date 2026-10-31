"""
Laboratory views — samples, test runs, results, dispositions, holds and recalls.
Rules live in services.py.
"""
import django_filters
from django.db.models import Count
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import viewsets, generics, status, permissions
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response

from apps.core.audit import record_audit
from apps.core.models import AuditEvent
from apps.core.permissions import role_permission
from apps.inventory.serializers import PackageListSerializer
from apps.receiving.serializers import AuditEventSerializer

from .models import (
    LabSample, LabTestRun, LabSampleAttachment, QualityDisposition, QualityHold,
    SampleStatus, DispositionDecision, ACTIVE_SAMPLE_STATUSES,
)
from .serializers import (
    LabSampleSerializer, LabSampleListSerializer, LabTestRunSerializer, LabTestResultSerializer,
    QualityDispositionSerializer, QualityHoldSerializer, LabSampleAttachmentSerializer,
    SampleCreateSerializer, CollectSerializer, ResultsInputSerializer, NotesSerializer,
    DispositionInputSerializer, ReasonInputSerializer, AttachmentUploadSerializer, HoldCreateSerializer,
)
from . import services

LabRoles = role_permission(*services.LAB_ROLES)
Reviewers = role_permission(*services.REVIEWER_ROLES)
# Lab reports / certificates: lab, quality, warehouse, management and audit
ReportReaders = role_permission('ADMIN', 'LAB_TECH', 'LAB_REVIEWER', 'WAREHOUSE', 'MANAGER', 'AUDITOR')


class SampleFilter(django_filters.FilterSet):
    status = django_filters.MultipleChoiceFilter(choices=SampleStatus.choices)
    package = django_filters.UUIDFilter(field_name='sample_packages__package')
    batch_number = django_filters.CharFilter(lookup_expr='iexact')
    active = django_filters.BooleanFilter(method='filter_active')
    overdue = django_filters.BooleanFilter(method='filter_overdue')

    class Meta:
        model = LabSample
        fields = ['status', 'material', 'batch_number', 'package']

    def filter_active(self, qs, name, value):
        return qs.filter(status__in=ACTIVE_SAMPLE_STATUSES) if value else qs.exclude(status__in=ACTIVE_SAMPLE_STATUSES)

    def filter_overdue(self, qs, name, value):
        overdue = qs.filter(status__in=ACTIVE_SAMPLE_STATUSES, required_by__lt=timezone.now())
        return overdue if value else qs.exclude(pk__in=overdue.values('pk'))


@extend_schema_view(list=extend_schema(tags=['laboratory']), retrieve=extend_schema(tags=['laboratory']),
                    create=extend_schema(tags=['laboratory'], request=SampleCreateSerializer, responses=LabSampleSerializer))
class LabSampleViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    filterset_class = SampleFilter
    search_fields = ['sample_id', 'material__code', 'material__name', 'batch_number', 'laboratory_name',
                     'sample_packages__package__package_id']
    ordering_fields = ['created_at', 'required_by', 'sample_id']
    ordering = ['-created_at']
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = LabSample.objects.select_related('material', 'sampled_by', 'sampled_unit') \
            .annotate(package_count=Count('sample_packages', distinct=True))
        if self.action == 'list':
            return qs
        return qs.prefetch_related(
            'sample_packages__package__unit', 'sample_packages__package__invoice_line__receipt',
            'test_runs__results__test_definition', 'test_runs__results__entered_by',
            'dispositions__decided_by', 'attachments__uploaded_by', 'requested_tests')

    def get_serializer_class(self):
        return LabSampleListSerializer if self.action == 'list' else LabSampleSerializer

    def _detail(self, sample, code=status.HTTP_200_OK):
        sample = self.get_queryset().get(pk=sample.pk)
        return Response(LabSampleSerializer(sample, context=self.get_serializer_context()).data, status=code)

    def create(self, request, *args, **kwargs):
        s = SampleCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(services.create_sample(s.validated_data, request.user, request=request),
                            status.HTTP_201_CREATED)

    @extend_schema(tags=['laboratory'], request=CollectSerializer, responses=LabSampleSerializer)
    @action(detail=True, methods=['post'])
    def collect(self, request, pk=None):
        s = CollectSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(services.collect_sample(self.get_object(), s.validated_data, request.user, request=request))

    @extend_schema(tags=['laboratory'], request=None, responses=LabSampleSerializer)
    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        return self._detail(services.receive_in_lab(self.get_object(), request.user, request=request))

    @extend_schema(tags=['laboratory'], request=NotesSerializer, responses=LabTestRunSerializer)
    @action(detail=True, methods=['post'], url_path='start-testing')
    def start_testing(self, request, pk=None):
        """Open a test run (run 1, or a new run for a retest — earlier runs are kept)."""
        s = NotesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        run = services.start_testing(self.get_object(), request.user, notes=s.validated_data['notes'], request=request)
        return Response(LabTestRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['laboratory'], request=DispositionInputSerializer, responses=QualityDispositionSerializer)
    @action(detail=True, methods=['post'])
    def disposition(self, request, pk=None):
        """Quality decision by a reviewer: RELEASED, REJECTED, ON_HOLD, RETEST or PARTIAL."""
        s = DispositionInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = services.decide(self.get_object(), s.validated_data, request.user, request=request)
        return Response(QualityDispositionSerializer(d).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['laboratory'], request=ReasonInputSerializer, responses=LabSampleSerializer)
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        s = ReasonInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        return self._detail(services.cancel_sample(self.get_object(), request.user,
                                                   reason=s.validated_data['reason'], request=request))

    @extend_schema(tags=['laboratory'], request=AttachmentUploadSerializer, responses=LabSampleAttachmentSerializer)
    @action(detail=True, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def attachments(self, request, pk=None):
        s = AttachmentUploadSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        att = services.attach_report(self.get_object(), s.validated_data['file'], s.validated_data['document_type'],
                                     request.user, notes=s.validated_data['notes'], request=request)
        return Response(LabSampleAttachmentSerializer(att).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['laboratory'], responses=AuditEventSerializer(many=True))
    @action(detail=True, methods=['get'])
    def timeline(self, request, pk=None):
        return Response(AuditEventSerializer(services.sample_timeline(self.get_object()), many=True).data)

    @extend_schema(tags=['laboratory'], responses=PackageListSerializer(many=True))
    @action(detail=False, methods=['get'], url_path='pending-packages')
    def pending_packages(self, request):
        """Packages in stock that still need a sample (no active sample covers them)."""
        qs = services.packages_awaiting_sampling().select_related(
            'material', 'unit', 'current_location', 'invoice_line__receipt').order_by('received_at')
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(PackageListSerializer(page, many=True).data)

    @extend_schema(tags=['laboratory'], responses=OpenApiTypes.OBJECT)
    @action(detail=False, methods=['get'])
    def dashboard(self, request):
        """Laboratory dashboard, from persisted state."""
        now = timezone.now()
        since = now - timezone.timedelta(days=30)
        by_status = dict(LabSample.objects.values_list('status').annotate(c=Count('id')))
        decisions = dict(QualityDisposition.objects.filter(decided_at__gte=since)
                         .values_list('decision').annotate(c=Count('id')))
        return Response({
            'by_status': {s: by_status.get(s, 0) for s in SampleStatus.values},
            'packages_awaiting_sampling': services.packages_awaiting_sampling().count(),
            'awaiting_collection': by_status.get(SampleStatus.REQUESTED, 0),
            'awaiting_tests': by_status.get(SampleStatus.COLLECTED, 0) + by_status.get(SampleStatus.RECEIVED_BY_LAB, 0),
            'in_progress': by_status.get(SampleStatus.TESTING, 0),
            'awaiting_review': by_status.get(SampleStatus.RESULTS_SUBMITTED, 0),
            'retest_queue': by_status.get(SampleStatus.RETEST_REQUIRED, 0),
            'overdue': LabSample.objects.filter(status__in=ACTIVE_SAMPLE_STATUSES, required_by__lt=now).count(),
            'decisions_30d': {d: decisions.get(d, 0) for d in DispositionDecision.values},
            'active_holds': QualityHold.objects.filter(is_active=True, hold_type='HOLD').count(),
            'active_recalls': QualityHold.objects.filter(is_active=True, hold_type='RECALL').count(),
        })


@extend_schema_view(list=extend_schema(tags=['laboratory']), retrieve=extend_schema(tags=['laboratory']))
class LabTestRunViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = LabTestRun.objects.select_related('sample', 'analyst', 'submitted_by', 'reviewed_by') \
        .prefetch_related('results__test_definition', 'results__entered_by')
    serializer_class = LabTestRunSerializer
    filterset_fields = ['sample', 'is_current', 'status']
    ordering = ['-created_at']

    def get_permissions(self):
        if self.action in ('results', 'submit'):
            return [LabRoles()]
        return [permissions.IsAuthenticated()]

    @extend_schema(tags=['laboratory'], request=ResultsInputSerializer, responses=LabTestResultSerializer(many=True))
    @action(detail=True, methods=['post'])
    def results(self, request, pk=None):
        """Enter or correct results while the run is open. Limits are evaluated server-side."""
        s = ResultsInputSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        saved = services.record_results(self.get_object(), s.validated_data['results'], request.user, request=request)
        return Response(LabTestResultSerializer(saved, many=True).data)

    @extend_schema(tags=['laboratory'], request=None, responses=LabTestRunSerializer)
    @action(detail=True, methods=['post'])
    def submit(self, request, pk=None):
        run = services.submit_run(self.get_object(), request.user, request=request)
        return Response(LabTestRunSerializer(self.get_queryset().get(pk=run.pk)).data)


@extend_schema_view(list=extend_schema(tags=['laboratory']), retrieve=extend_schema(tags=['laboratory']))
class QualityDispositionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = QualityDisposition.objects.select_related('sample', 'decided_by', 'test_run')
    serializer_class = QualityDispositionSerializer
    filterset_fields = ['decision', 'sample']
    ordering = ['-decided_at']


@extend_schema_view(list=extend_schema(tags=['laboratory']), retrieve=extend_schema(tags=['laboratory']),
                    create=extend_schema(tags=['laboratory'], request=HoldCreateSerializer, responses=QualityHoldSerializer))
class QualityHoldViewSet(viewsets.ModelViewSet):
    http_method_names = ['get', 'post', 'head', 'options']
    queryset = QualityHold.objects.select_related('held_by', 'released_by', 'material').prefetch_related('packages')
    serializer_class = QualityHoldSerializer
    filterset_fields = ['is_active', 'hold_type', 'material']
    search_fields = ['reference', 'batch_number', 'packages__package_id']

    def get_permissions(self):
        if self.action in ('create', 'release'):
            return [Reviewers()]
        return [permissions.IsAuthenticated()]

    def create(self, request, *args, **kwargs):
        s = HoldCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        hold = services.create_hold(s.validated_data, request.user, request=request)
        return Response(QualityHoldSerializer(hold).data, status=status.HTTP_201_CREATED)

    @extend_schema(tags=['laboratory'], request=NotesSerializer, responses=QualityHoldSerializer)
    @action(detail=True, methods=['post'])
    def release(self, request, pk=None):
        s = NotesSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        hold = services.release_hold(self.get_object(), request.user, notes=s.validated_data['notes'], request=request)
        return Response(QualityHoldSerializer(hold).data)

    @extend_schema(tags=['laboratory'], responses=OpenApiTypes.OBJECT)
    @action(detail=True, methods=['get'])
    def impact(self, request, pk=None):
        """Forward traceability: remaining stock and factory issues of the held / recalled material."""
        return Response(services.hold_impact(self.get_object()))


@extend_schema(tags=['laboratory'], responses={(200, 'application/octet-stream'): OpenApiTypes.BINARY})
class LabAttachmentDownloadView(generics.GenericAPIView):
    permission_classes = [ReportReaders]
    queryset = LabSampleAttachment.objects.select_related('sample')
    serializer_class = LabSampleAttachmentSerializer

    def get(self, request, pk):
        att = get_object_or_404(self.get_queryset(), pk=pk)
        record_audit(request.user, AuditEvent.ACTION_DOWNLOAD, 'LabSample', att.sample_id,
                     extra={'attachment': str(att.id)}, request=request)
        response = FileResponse(att.file.open('rb'), as_attachment=True, filename=att.original_filename,
                                content_type=att.mime_type or 'application/octet-stream')
        response['X-Content-Type-Options'] = 'nosniff'
        return response
