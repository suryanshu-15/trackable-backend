"""
Laboratory serializers. Read shapes are model serializers; every write goes
through an input serializer and the service layer.
"""
from decimal import Decimal

from django.urls import reverse
from rest_framework import serializers

from apps.inventory.models import Package
from apps.master.models import LabTestDefinition, Material, UnitOfMeasure
from .models import (
    LabSample, SamplePackage, LabTestRun, LabTestResult, LabSampleAttachment,
    QualityDisposition, QualityHold, DispositionDecision, HoldType, TestResultStatus, LabDocumentType,
)
from . import services


class SamplePackageSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)
    batch_number = serializers.CharField(source='package.batch_number', read_only=True)
    qc_status = serializers.CharField(source='package.qc_status', read_only=True)
    current_quantity = serializers.DecimalField(source='package.current_quantity', max_digits=15, decimal_places=4, read_only=True)
    unit_symbol = serializers.CharField(source='package.unit.symbol', read_only=True)
    receipt_number = serializers.CharField(source='package.invoice_line.receipt.receipt_number', read_only=True)

    class Meta:
        model = SamplePackage
        fields = ['id', 'package', 'package_code', 'batch_number', 'qc_status', 'current_quantity', 'unit_symbol',
                  'receipt_number', 'drawn_quantity', 'notes']
        read_only_fields = fields


class LabTestResultSerializer(serializers.ModelSerializer):
    test_code = serializers.CharField(source='test_definition.code', read_only=True)
    test_name = serializers.CharField(source='test_definition.name', read_only=True)
    is_required = serializers.BooleanField(source='test_definition.is_required', read_only=True)
    result_unit_symbol = serializers.CharField(source='result_unit.symbol', read_only=True, default=None)
    entered_by_name = serializers.CharField(source='entered_by.display_name', read_only=True, default=None)

    class Meta:
        model = LabTestResult
        fields = [
            'id', 'test_run', 'test_definition', 'test_code', 'test_name', 'is_required',
            'numeric_result', 'text_result', 'boolean_result', 'result_unit', 'result_unit_symbol',
            'min_limit', 'max_limit', 'acceptable_values', 'result_status', 'interpretation',
            'remarks', 'revision', 'entered_at', 'entered_by', 'entered_by_name', 'updated_at',
        ]
        read_only_fields = fields


class LabTestRunSerializer(serializers.ModelSerializer):
    results = LabTestResultSerializer(many=True, read_only=True)
    analyst_name = serializers.CharField(source='analyst.display_name', read_only=True, default=None)
    submitted_by_name = serializers.CharField(source='submitted_by.display_name', read_only=True, default=None)
    reviewed_by_name = serializers.CharField(source='reviewed_by.display_name', read_only=True, default=None)
    outcome = serializers.SerializerMethodField()
    missing_required_tests = serializers.SerializerMethodField()

    class Meta:
        model = LabTestRun
        fields = [
            'id', 'sample', 'run_number', 'status', 'is_current', 'started_at',
            'analyst', 'analyst_name', 'submitted_at', 'submitted_by', 'submitted_by_name',
            'reviewed_at', 'reviewed_by', 'reviewed_by_name', 'notes',
            'results', 'outcome', 'missing_required_tests', 'created_at',
        ]
        read_only_fields = fields

    def get_outcome(self, obj) -> str:
        return services.run_outcome(obj)

    def get_missing_required_tests(self, obj) -> list[str]:
        return [t.code for t in services.missing_required_tests(obj)]


class QualityDispositionSerializer(serializers.ModelSerializer):
    decided_by_name = serializers.CharField(source='decided_by.display_name', read_only=True)
    sample_code = serializers.CharField(source='sample.sample_id', read_only=True)
    run_number = serializers.IntegerField(source='test_run.run_number', read_only=True, default=None)
    decision_display = serializers.CharField(source='get_decision_display', read_only=True)

    class Meta:
        model = QualityDisposition
        fields = [
            'id', 'sample', 'sample_code', 'test_run', 'run_number', 'decision', 'decision_display',
            'decided_by', 'decided_by_name', 'decided_at', 'reason', 'notes',
            'released_package_ids', 'affected_package_ids', 'remainder_decision', 'new_retest_date', 'new_expiry_date',
        ]
        read_only_fields = fields


class LabSampleAttachmentSerializer(serializers.ModelSerializer):
    download_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(source='uploaded_by.display_name', read_only=True, default=None)

    class Meta:
        model = LabSampleAttachment
        fields = ['id', 'sample', 'document_type', 'original_filename', 'file_size_bytes', 'mime_type',
                  'notes', 'uploaded_by', 'uploaded_by_name', 'download_url', 'created_at']
        read_only_fields = fields

    def get_download_url(self, obj) -> str:
        return reverse('lab-attachment-download', kwargs={'pk': obj.pk})


class LabSampleListSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.code', read_only=True)
    material_name = serializers.CharField(source='material.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    package_count = serializers.IntegerField(read_only=True)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = LabSample
        fields = [
            'id', 'sample_id', 'material', 'material_code', 'material_name', 'batch_number',
            'status', 'status_display', 'sampled_at', 'laboratory_name', 'package_count',
            'required_by', 'is_overdue', 'created_at',
        ]
        read_only_fields = fields

    def get_is_overdue(self, obj) -> bool:
        from django.utils import timezone
        from .models import ACTIVE_SAMPLE_STATUSES
        return bool(obj.required_by and obj.required_by < timezone.now() and obj.status in ACTIVE_SAMPLE_STATUSES)


class LabSampleSerializer(LabSampleListSerializer):
    sample_packages = SamplePackageSerializer(many=True, read_only=True)
    requested_tests_detail = serializers.SerializerMethodField()
    test_runs = LabTestRunSerializer(many=True, read_only=True)
    dispositions = QualityDispositionSerializer(many=True, read_only=True)
    attachments = LabSampleAttachmentSerializer(many=True, read_only=True)
    sampled_by_name = serializers.CharField(source='sampled_by.display_name', read_only=True, default=None)
    sampled_unit_symbol = serializers.CharField(source='sampled_unit.symbol', read_only=True, default=None)
    allowed_actions = serializers.SerializerMethodField()

    class Meta(LabSampleListSerializer.Meta):
        fields = LabSampleListSerializer.Meta.fields + [
            'sampled_quantity', 'sampled_unit', 'sampled_unit_symbol', 'sampled_by', 'sampled_by_name',
            'received_by_lab_at', 'analyst_name', 'sample_condition', 'specification', 'notes',
            'completed_at', 'cancel_reason', 'requested_tests', 'requested_tests_detail',
            'sample_packages', 'test_runs', 'dispositions', 'attachments', 'allowed_actions', 'updated_at',
        ]
        read_only_fields = fields

    def get_requested_tests_detail(self, obj) -> list[dict]:
        return [{'id': t.pk, 'code': t.code, 'name': t.name, 'result_type': t.result_type,
                 'unit': t.unit.symbol if t.unit_id else None, 'min_value': t.min_value, 'max_value': t.max_value,
                 'acceptable_text_values': t.acceptable_text_values, 'is_required': t.is_required}
                for t in obj.requested_tests.select_related('unit')]

    def get_allowed_actions(self, obj) -> list[str]:
        from .models import SampleStatus as S
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None:
            return []
        actions = []
        if user.has_role(*services.LAB_ROLES):
            actions += {S.REQUESTED: ['collect', 'cancel'], S.COLLECTED: ['receive', 'start_testing', 'cancel'],
                        S.RECEIVED_BY_LAB: ['start_testing', 'cancel'], S.RETEST_REQUIRED: ['start_testing']}.get(obj.status, [])
            if obj.status == S.TESTING:
                actions += ['enter_results', 'submit_results']
            if obj.status not in (S.CANCELLED,):
                actions.append('attach_report')
        if obj.status == S.RESULTS_SUBMITTED and user.has_role(*services.REVIEWER_ROLES):
            actions.append('decide')
        return actions


# ---------------------------------------------------------------------------
# Input serializers
# ---------------------------------------------------------------------------

class SampleCreateSerializer(serializers.Serializer):
    packages = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all(), many=True)
    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all(), required=False)
    requested_tests = serializers.PrimaryKeyRelatedField(
        queryset=LabTestDefinition.objects.filter(is_active=True), many=True, required=False)
    laboratory_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    analyst_name = serializers.CharField(max_length=200, required=False, allow_blank=True)
    specification = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    required_by = serializers.DateTimeField(required=False, allow_null=True)


class DrawSerializer(serializers.Serializer):
    package = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all())
    quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))


class CollectSerializer(serializers.Serializer):
    draws = DrawSerializer(many=True, required=False)
    sampled_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0'),
                                                required=False, allow_null=True)
    sampled_unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.all(), required=False, allow_null=True)
    sampled_at = serializers.DateTimeField(required=False, allow_null=True)
    sample_condition = serializers.CharField(required=False, allow_blank=True, default='')


class ResultEntrySerializer(serializers.Serializer):
    test_definition = serializers.PrimaryKeyRelatedField(queryset=LabTestDefinition.objects.all())
    numeric_result = serializers.DecimalField(max_digits=20, decimal_places=6, required=False, allow_null=True)
    text_result = serializers.CharField(max_length=500, required=False, allow_blank=True)
    boolean_result = serializers.BooleanField(required=False, allow_null=True)
    result_unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.all(), required=False, allow_null=True)
    result_status = serializers.ChoiceField(choices=TestResultStatus.choices, required=False)
    interpretation = serializers.CharField(required=False, allow_blank=True)
    remarks = serializers.CharField(required=False, allow_blank=True)


class ResultsInputSerializer(serializers.Serializer):
    results = ResultEntrySerializer(many=True, min_length=1)


class NotesSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class DispositionInputSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=DispositionDecision.choices)
    reason = serializers.CharField()
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    released_packages = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all(), many=True, required=False)
    remainder_decision = serializers.ChoiceField(
        choices=[DispositionDecision.REJECTED, DispositionDecision.ON_HOLD], required=False)
    new_retest_date = serializers.DateField(required=False, allow_null=True)
    new_expiry_date = serializers.DateField(required=False, allow_null=True)


class ReasonInputSerializer(serializers.Serializer):
    reason = serializers.CharField()


class AttachmentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    document_type = serializers.ChoiceField(choices=LabDocumentType.choices)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class QualityHoldSerializer(serializers.ModelSerializer):
    package_ids = serializers.SerializerMethodField()
    held_by_name = serializers.CharField(source='held_by.display_name', read_only=True)
    released_by_name = serializers.CharField(source='released_by.display_name', read_only=True, default=None)
    material_code = serializers.CharField(source='material.code', read_only=True, default=None)

    class Meta:
        model = QualityHold
        fields = ['id', 'reference', 'hold_type', 'material', 'material_code', 'batch_number', 'reason',
                  'package_ids', 'held_by', 'held_by_name', 'held_at', 'is_active',
                  'released_by', 'released_by_name', 'released_at', 'release_notes']
        read_only_fields = fields

    def get_package_ids(self, obj) -> list[str]:
        return list(obj.packages.order_by('package_id').values_list('package_id', flat=True))


class HoldCreateSerializer(serializers.Serializer):
    hold_type = serializers.ChoiceField(choices=HoldType.choices, default=HoldType.HOLD)
    packages = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all(), many=True, required=False)
    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all(), required=False)
    batch_number = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    reason = serializers.CharField()
