"""
Serializers for Receiving app.
"""
from decimal import Decimal

from rest_framework import serializers
from django.urls import reverse
from .models import (
    GoodsReceipt, InvoiceLine, ReceiptDocument,
    ReceiptLineVerification, DocumentType, WeighingMethod,
)
from . import services
from apps.core.models import AuditEvent
from apps.master.models import Material, UnitOfMeasure
from apps.master.serializers import SupplierSerializer


class ReceiptDocumentSerializer(serializers.ModelSerializer):
    """Read shape. Files are never exposed by storage URL, only via the protected download endpoint."""
    download_url = serializers.SerializerMethodField()
    uploaded_by_name = serializers.CharField(source='uploaded_by.display_name', read_only=True, default=None)

    class Meta:
        model = ReceiptDocument
        fields = [
            'id', 'receipt', 'document_type', 'original_filename', 'file_size_bytes',
            'mime_type', 'notes', 'uploaded_by', 'uploaded_by_name', 'download_url', 'created_at',
        ]
        read_only_fields = fields

    def get_download_url(self, obj) -> str:
        return reverse('receipt-document-download', kwargs={'pk': obj.pk})


class ReceiptDocumentUploadSerializer(serializers.Serializer):
    file = serializers.FileField()
    document_type = serializers.ChoiceField(choices=DocumentType.choices)
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class InvoiceLineSerializer(serializers.ModelSerializer):
    material_name = serializers.CharField(source='material.name', read_only=True)
    declared_unit_symbol = serializers.CharField(source='declared_unit.symbol', read_only=True)
    has_verification = serializers.SerializerMethodField()
    verification_status = serializers.CharField(source='verification.verification_status', read_only=True, default=None)
    accepted_quantity = serializers.DecimalField(
        source='verification.accepted_quantity', max_digits=15, decimal_places=4, read_only=True, default=None)
    package_count = serializers.SerializerMethodField()

    class Meta:
        model = InvoiceLine
        fields = [
            'id', 'receipt', 'line_number', 'material', 'material_name',
            'material_code_declared', 'material_name_declared', 'description',
            'grade_or_specification', 'declared_quantity', 'declared_unit',
            'declared_unit_symbol', 'declared_weight', 'declared_weight_unit',
            'batch_number', 'manufacturing_date', 'expiry_date', 'retest_date',
            'required_tests', 'quality_specification', 'remarks',
            'has_verification', 'verification_status', 'accepted_quantity', 'package_count',
            'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'has_verification', 'created_at', 'updated_at']
        # (receipt, line_number) uniqueness is enforced by the service, which can auto-number
        validators = []
        extra_kwargs = {
            'line_number': {'required': False},
            'material_name_declared': {'required': False},
        }

    def get_has_verification(self, obj) -> bool:
        return hasattr(obj, 'verification')

    def get_package_count(self, obj) -> int:
        return obj.packages.filter(parent_package__isnull=True).count()

    def validate(self, attrs):
        if self.instance is not None:
            attrs.pop('receipt', None)  # a line cannot be moved to another receipt
        material = attrs.get('material', getattr(self.instance, 'material', None))
        name = attrs.get('material_name_declared', getattr(self.instance, 'material_name_declared', ''))
        if not material and not name:
            raise serializers.ValidationError(
                {'material_name_declared': 'Provide a material or the declared material name.'}
            )
        qty = attrs.get('declared_quantity')
        if qty is not None and qty <= 0:
            raise serializers.ValidationError({'declared_quantity': 'Must be greater than zero.'})
        if attrs.get('declared_weight') is not None and not attrs.get('declared_weight_unit', getattr(self.instance, 'declared_weight_unit', None)):
            raise serializers.ValidationError({'declared_weight_unit': 'A unit is required when a weight is given.'})
        return attrs


class GoodsReceiptListSerializer(serializers.ModelSerializer):
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    line_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = GoodsReceipt
        fields = [
            'id', 'receipt_number', 'supplier', 'supplier_name',
            'invoice_number', 'invoice_date', 'vehicle_number',
            'arrival_datetime', 'status', 'status_display',
            'net_weight', 'line_count', 'created_at',
        ]
        read_only_fields = ['id', 'receipt_number', 'status', 'created_at']


class GoodsReceiptSerializer(serializers.ModelSerializer):
    supplier_detail = SupplierSerializer(source='supplier', read_only=True)
    weight_unit_symbol = serializers.CharField(source='weight_unit.symbol', read_only=True, default=None)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    invoice_lines = InvoiceLineSerializer(many=True, read_only=True)
    documents = ReceiptDocumentSerializer(many=True, read_only=True)
    allowed_transitions = serializers.SerializerMethodField()
    duplicate_warning = serializers.SerializerMethodField()
    weight_discrepancy = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, default=None)

    class Meta:
        model = GoodsReceipt
        fields = [
            'id', 'receipt_number', 'supplier', 'supplier_detail',
            'invoice_number', 'invoice_date', 'vehicle_number', 'driver_name',
            'arrival_datetime', 'gross_weight', 'tare_weight', 'net_weight',
            'weight_unit', 'weight_unit_symbol', 'weighing_method', 'weighbridge_reference',
            'purchase_order_number', 'delivery_challan_number',
            'status', 'status_display', 'gate_notes',
            'gate_registered_at', 'unloading_started_at',
            'unloading_completed_at', 'stored_at', 'closed_at',
            'invoice_lines', 'documents', 'allowed_transitions',
            'duplicate_warning', 'weight_discrepancy',
            'created_by', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'receipt_number', 'net_weight', 'status', 'gate_registered_at',
            'unloading_started_at', 'unloading_completed_at',
            'stored_at', 'closed_at', 'created_by', 'created_at', 'updated_at',
        ]

    def get_allowed_transitions(self, obj) -> list[str]:
        request = self.context.get('request')
        if not request:
            return []
        return services.allowed_receipt_transitions(obj, request.user)

    def get_duplicate_warning(self, obj) -> list[str]:
        """Other non-cancelled receipts matching the configured duplicate key."""
        dupes = services.find_duplicate_receipts(
            supplier=obj.supplier, invoice_number=obj.invoice_number,
            invoice_date=obj.invoice_date, exclude_pk=obj.pk,
        )
        return list(dupes.values_list('receipt_number', flat=True)[:5])

    def get_weight_discrepancy(self, obj) -> dict | None:
        return services.weight_discrepancy(obj)

    def validate(self, attrs):
        gross = attrs.get('gross_weight', getattr(self.instance, 'gross_weight', None))
        tare = attrs.get('tare_weight', getattr(self.instance, 'tare_weight', None))
        if gross is not None and tare is not None and gross < tare:
            raise serializers.ValidationError({'tare_weight': 'Tare weight cannot exceed gross weight.'})
        unit = attrs.get('weight_unit', getattr(self.instance, 'weight_unit', None))
        if (gross is not None or tare is not None) and unit is None:
            raise serializers.ValidationError({'weight_unit': 'A weight unit is required when weights are given.'})
        return attrs


class WeightCorrectionSerializer(serializers.Serializer):
    gross_weight = serializers.DecimalField(max_digits=15, decimal_places=4, required=False, allow_null=True, min_value=Decimal("0"))
    tare_weight = serializers.DecimalField(max_digits=15, decimal_places=4, required=False, allow_null=True, min_value=Decimal("0"))
    weight_unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.all(), required=False)
    weighing_method = serializers.ChoiceField(choices=WeighingMethod.choices, required=False)
    weighbridge_reference = serializers.CharField(max_length=100, required=False, allow_blank=True)
    reason = serializers.CharField()


class TransitionSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class AuditEventSerializer(serializers.ModelSerializer):
    actor_name = serializers.CharField(source='actor.display_name', read_only=True, default=None)

    class Meta:
        model = AuditEvent
        fields = [
            'id', 'timestamp', 'actor', 'actor_name', 'action', 'entity_type', 'entity_id',
            'reason', 'before_state', 'after_state', 'extra',
        ]
        read_only_fields = fields


class ReceiptLineVerificationSerializer(serializers.ModelSerializer):
    """Read shape. Writes go through VerificationInputSerializer + services.record_verification."""
    invoice_line_display = serializers.CharField(source='invoice_line.__str__', read_only=True)
    received_unit_symbol = serializers.CharField(source='received_unit.symbol', read_only=True, default=None)
    verified_by_name = serializers.CharField(source='verified_by.display_name', read_only=True, default=None)
    exception_authorized_by_name = serializers.CharField(
        source='exception_authorized_by.display_name', read_only=True, default=None)
    packaged_quantity = serializers.SerializerMethodField()

    class Meta:
        model = ReceiptLineVerification
        fields = [
            'id', 'invoice_line', 'invoice_line_display',
            'received_quantity', 'received_unit', 'received_unit_symbol', 'accepted_quantity',
            'short_quantity', 'excess_quantity', 'damaged_quantity', 'mismatched_quantity',
            'identified_material', 'observed_description', 'observed_grade',
            'unloading_start', 'unloading_end',
            'verification_status', 'discrepancy_status', 'discrepancy_notes',
            'exception_authorized_by', 'exception_authorized_by_name', 'exception_reason',
            'verified_by', 'verified_by_name', 'verification_notes', 'packaged_quantity',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_packaged_quantity(self, obj) -> str:
        from apps.inventory.services import packaged_quantity
        return str(packaged_quantity(obj.invoice_line))


class VerificationInputSerializer(serializers.Serializer):
    received_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0'))
    received_unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.filter(is_active=True))
    accepted_quantity = serializers.DecimalField(
        max_digits=15, decimal_places=4, min_value=Decimal('0'), required=False, allow_null=True)
    damaged_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0'), required=False)
    mismatched_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0'), required=False)
    identified_material = serializers.PrimaryKeyRelatedField(
        queryset=Material.objects.filter(is_active=True), required=False, allow_null=True)
    observed_description = serializers.CharField(required=False, allow_blank=True)
    observed_grade = serializers.CharField(max_length=200, required=False, allow_blank=True)
    unloading_start = serializers.DateTimeField(required=False, allow_null=True)
    unloading_end = serializers.DateTimeField(required=False, allow_null=True)
    verification_notes = serializers.CharField(required=False, allow_blank=True)
    discrepancy_notes = serializers.CharField(required=False, allow_blank=True)


class ReasonSerializer(serializers.Serializer):
    reason = serializers.CharField()
