"""
Inventory serializers.
"""
from drf_spectacular.utils import extend_schema_field, extend_schema_serializer
from rest_framework import serializers
from decimal import Decimal

from apps.master.models import StorageLocation, Material, UnitOfMeasure, Factory, Department
from apps.users.models import User
from .models import (
    Package, PackageLabelEvent, InventoryMovement,
    StockAdjustment, MaterialRequest, MaterialRequestLine, StockReservation, MaterialIssue,
    MaterialIssueLine, MaterialReturn, DisposalRecord, DisposalType,
    PackageStatus,
)


class PackageLabelEventSerializer(serializers.ModelSerializer):
    printed_by_name = serializers.CharField(source='printed_by.display_name', read_only=True, default=None)

    class Meta:
        model = PackageLabelEvent
        fields = ['id', 'event_type', 'printed_by', 'printed_by_name', 'printer_reference', 'reprint_reason', 'created_at']
        read_only_fields = fields


class PackageListSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.code', read_only=True)
    material_name = serializers.CharField(source='material.name', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    location_code = serializers.CharField(source='current_location.code', read_only=True, default=None)
    location_is_quarantine = serializers.BooleanField(source='current_location.is_quarantine', read_only=True, default=None)
    receipt_number = serializers.CharField(source='invoice_line.receipt.receipt_number', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    qc_status_display = serializers.CharField(source='get_qc_status_display', read_only=True)
    is_available = serializers.BooleanField(read_only=True)
    requires_quarantine = serializers.BooleanField(read_only=True)

    class Meta:
        model = Package
        fields = [
            'id', 'package_id', 'material', 'material_code', 'material_name',
            'original_quantity', 'current_quantity', 'unit_symbol',
            'status', 'status_display', 'qc_status', 'qc_status_display',
            'is_on_hold', 'is_recalled', 'is_available', 'requires_quarantine',
            'batch_number', 'expiry_date', 'retest_date', 'received_at',
            'current_location', 'location_code', 'location_is_quarantine',
            'receipt_number', 'parent_package', 'label_print_count', 'created_at',
        ]
        read_only_fields = fields


class PackageRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    package_id = serializers.CharField()


class PackageSampleRefSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    sample_id = serializers.CharField()
    status = serializers.CharField()
    drawn_quantity = serializers.CharField(allow_null=True)


class PackageSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.code', read_only=True)
    material_name = serializers.CharField(source='material.name', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    location_display = serializers.SerializerMethodField()
    location_is_quarantine = serializers.BooleanField(source='current_location.is_quarantine', read_only=True, default=None)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    qc_status_display = serializers.CharField(source='get_qc_status_display', read_only=True)
    supplier_name = serializers.CharField(source='supplier.name', read_only=True)
    receipt = serializers.UUIDField(source='invoice_line.receipt_id', read_only=True)
    receipt_number = serializers.CharField(source='invoice_line.receipt.receipt_number', read_only=True)
    invoice_line_number = serializers.IntegerField(source='invoice_line.line_number', read_only=True)
    parent_package_code = serializers.CharField(source='parent_package.package_id', read_only=True, default=None)
    child_packages = serializers.SerializerMethodField()
    is_available = serializers.BooleanField(read_only=True)
    blocking_reasons = serializers.SerializerMethodField()
    reserved_quantity = serializers.SerializerMethodField()
    requires_quarantine = serializers.BooleanField(read_only=True)
    label_events = PackageLabelEventSerializer(many=True, read_only=True)
    allowed_actions = serializers.SerializerMethodField()
    samples = serializers.SerializerMethodField()

    class Meta:
        model = Package
        fields = [
            'id', 'package_id',
            'receipt', 'receipt_number', 'invoice_line', 'invoice_line_number',
            'parent_package', 'parent_package_code', 'child_packages', 'split_reason',
            'material', 'material_code', 'material_name', 'supplier', 'supplier_name',
            'original_quantity', 'current_quantity', 'unit', 'unit_symbol',
            'batch_number', 'manufacturing_date', 'expiry_date', 'retest_date', 'grade', 'received_at',
            'status', 'status_display', 'current_location', 'location_display', 'location_is_quarantine',
            'qc_status', 'qc_status_display', 'is_on_hold', 'is_recalled',
            'is_available', 'blocking_reasons', 'requires_quarantine', 'reserved_quantity',
            'notes', 'label_printed_at', 'label_print_count', 'label_events', 'allowed_actions', 'samples',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_location_display(self, obj) -> str | None:
        return str(obj.current_location) if obj.current_location_id else None

    @extend_schema_field(PackageRefSerializer(many=True))
    def get_child_packages(self, obj):
        return [{'id': str(pk), 'package_id': code}
                for pk, code in obj.child_packages.order_by('package_id').values_list('id', 'package_id')]

    def get_blocking_reasons(self, obj) -> list[str]:
        return obj.blocking_reasons()

    def get_reserved_quantity(self, obj) -> str:
        return str(sum((r.reserved_quantity for r in obj.reservations.filter(is_active=True)), Decimal('0')))

    @extend_schema_field(PackageSampleRefSerializer(many=True))
    def get_samples(self, obj):
        return [{'id': str(sp.sample_id), 'sample_id': sp.sample.sample_id, 'status': sp.sample.status,
                 'drawn_quantity': str(sp.drawn_quantity) if sp.drawn_quantity is not None else None}
                for sp in obj.sample_packages.select_related('sample').order_by('created_at')]

    def get_allowed_actions(self, obj) -> list[str]:
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None:
            return []
        actions = []
        if obj.status in (PackageStatus.CREATED, PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED) \
                and user.has_role('ADMIN', 'RECEIVING', 'WAREHOUSE'):
            actions.append('print_label' if obj.label_print_count == 0 else 'reprint_label')
        if obj.status == PackageStatus.AWAITING_PUTAWAY and user.has_role('ADMIN', 'RECEIVING', 'WAREHOUSE'):
            actions.append('putaway')
        if obj.status == PackageStatus.STORED and user.has_role('ADMIN', 'WAREHOUSE'):
            actions.append('transfer')
        if obj.status in (PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED) and obj.current_quantity > 0 \
                and user.has_role('ADMIN', 'WAREHOUSE'):
            actions.append('split')
        if obj.status in (PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED) and user.has_role(
                'ADMIN', 'WAREHOUSE', 'LAB_REVIEWER') \
                and not [r for r in obj.blocking_reasons() if not r.startswith('status is')]:
            actions.append('print_release_tag')
        return actions


class PackageSpecSerializer(serializers.Serializer):
    quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))
    batch_number = serializers.CharField(max_length=200, required=False, allow_blank=True)
    manufacturing_date = serializers.DateField(required=False, allow_null=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    retest_date = serializers.DateField(required=False, allow_null=True)
    grade = serializers.CharField(max_length=200, required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class PackageCreateRequestSerializer(serializers.Serializer):
    """Either an explicit `packages` list, or `count` identical packages of `quantity_each`."""
    packages = PackageSpecSerializer(many=True, required=False)
    count = serializers.IntegerField(min_value=1, max_value=500, required=False)
    quantity_each = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'), required=False)
    batch_number = serializers.CharField(max_length=200, required=False, allow_blank=True)
    expiry_date = serializers.DateField(required=False, allow_null=True)
    retest_date = serializers.DateField(required=False, allow_null=True)
    manufacturing_date = serializers.DateField(required=False, allow_null=True)

    def validate(self, attrs):
        if attrs.get('packages'):
            if attrs.get('count') or attrs.get('quantity_each'):
                raise serializers.ValidationError('Send either `packages` or `count` + `quantity_each`, not both.')
            return {'specs': [dict(p) for p in attrs['packages']]}
        if not (attrs.get('count') and attrs.get('quantity_each')):
            raise serializers.ValidationError('Send `packages`, or `count` and `quantity_each`.')
        common = {k: attrs[k] for k in ('batch_number', 'expiry_date', 'retest_date', 'manufacturing_date') if attrs.get(k)}
        return {'specs': [{'quantity': attrs['quantity_each'], **common} for _ in range(attrs['count'])]}


class LabelPrintSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
    printer_reference = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')


class LocationMoveSerializer(serializers.Serializer):
    location = serializers.PrimaryKeyRelatedField(queryset=StorageLocation.objects.all())
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class SplitSerializer(serializers.Serializer):
    quantities = serializers.ListField(
        child=serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001')),
        min_length=1, max_length=100,
    )
    reason = serializers.CharField()


class LabelDataSerializer(serializers.Serializer):
    package_id = serializers.CharField()
    package_type = serializers.CharField()
    qr_payload = serializers.CharField()
    barcode_value = serializers.CharField()
    material_code = serializers.CharField()
    material_name = serializers.CharField()
    quantity = serializers.CharField()
    unit = serializers.CharField()
    batch_number = serializers.CharField()
    grade = serializers.CharField()
    manufacturing_date = serializers.DateField(allow_null=True)
    expiry_date = serializers.DateField(allow_null=True)
    retest_date = serializers.DateField(allow_null=True)
    supplier = serializers.CharField()
    receipt_number = serializers.CharField()
    line_number = serializers.IntegerField()
    received_date = serializers.DateField()
    parent_package_id = serializers.CharField(allow_null=True)


class TimelineEventSerializer(serializers.Serializer):
    timestamp = serializers.DateTimeField()
    kind = serializers.CharField()
    title = serializers.CharField()
    actor = serializers.CharField(allow_null=True)
    details = serializers.DictField()


class InventoryMovementSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)
    from_location_display = serializers.SerializerMethodField()
    to_location_display = serializers.SerializerMethodField()
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    reason_display = serializers.CharField(source='get_reason_display', read_only=True)
    performed_by_name = serializers.CharField(source='performed_by.display_name', read_only=True, default=None)

    class Meta:
        model = InventoryMovement
        fields = [
            'id', 'package', 'package_code', 'from_location', 'from_location_display',
            'to_location', 'to_location_display', 'location_changed',
            'quantity', 'quantity_delta', 'balance_after', 'unit', 'unit_symbol',
            'reason', 'reason_display', 'performed_by', 'performed_by_name', 'notes', 'reference_id', 'created_at',
        ]
        read_only_fields = fields

    def get_from_location_display(self, obj) -> str | None:
        return str(obj.from_location) if obj.from_location_id else None

    def get_to_location_display(self, obj) -> str | None:
        return str(obj.to_location) if obj.to_location_id else None


class StockAdjustmentSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    created_by_name = serializers.CharField(source='created_by.display_name', read_only=True, default=None)
    approved_by_name = serializers.CharField(source='approved_by.display_name', read_only=True, default=None)

    class Meta:
        model = StockAdjustment
        fields = ['id', 'package', 'package_code', 'adjustment_type', 'quantity_change', 'unit', 'unit_symbol',
                  'reason', 'reference', 'status', 'applied_delta', 'created_by', 'created_by_name', 'created_at',
                  'approved_by', 'approved_by_name', 'approved_at', 'approval_notes']
        read_only_fields = fields


class StockAdjustmentInputSerializer(serializers.Serializer):
    package = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all())
    adjustment_type = serializers.ChoiceField(choices=StockAdjustment.ADJUSTMENT_TYPE_CHOICES)
    quantity_change = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0'))
    unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.all())
    reason = serializers.CharField()
    reference = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')


class DecisionNotesSerializer(serializers.Serializer):
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class ReservationSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)

    class Meta:
        model = StockReservation
        fields = ['id', 'package', 'package_code', 'reserved_quantity', 'is_active', 'created_at', 'released_at']
        read_only_fields = fields


class MaterialRequestLineSerializer(serializers.ModelSerializer):
    material_code = serializers.CharField(source='material.code', read_only=True)
    material_name = serializers.CharField(source='material.name', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    remaining_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, read_only=True)
    reservations = serializers.SerializerMethodField()

    class Meta:
        model = MaterialRequestLine
        fields = ['id', 'material', 'material_code', 'material_name', 'requested_quantity', 'issued_quantity',
                  'remaining_quantity', 'unit', 'unit_symbol', 'notes', 'reservations']
        read_only_fields = fields

    @extend_schema_field(ReservationSerializer(many=True))
    def get_reservations(self, obj):
        return ReservationSerializer(obj.reservations.filter(is_active=True).select_related('package'), many=True).data


@extend_schema_serializer(component_name='FactoryMaterialRequest')
class MaterialRequestSerializer(serializers.ModelSerializer):
    factory_name = serializers.CharField(source='factory.name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True, default=None)
    requested_by_name = serializers.CharField(source='requested_by.display_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    lines = MaterialRequestLineSerializer(many=True, read_only=True)
    issue_numbers = serializers.SerializerMethodField()

    class Meta:
        model = MaterialRequest
        fields = ['id', 'request_number', 'factory', 'factory_name', 'department', 'department_name',
                  'requested_by', 'requested_by_name', 'required_by', 'production_order', 'cost_centre', 'notes',
                  'status', 'status_display', 'closed_reason', 'lines', 'issue_numbers', 'created_at', 'updated_at']
        read_only_fields = fields

    def get_issue_numbers(self, obj) -> list[str]:
        return list(obj.issues.values_list('issue_number', flat=True))


class RequestLineInputSerializer(serializers.Serializer):
    material = serializers.PrimaryKeyRelatedField(queryset=Material.objects.all())
    quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))
    unit = serializers.PrimaryKeyRelatedField(queryset=UnitOfMeasure.objects.all())
    notes = serializers.CharField(max_length=500, required=False, allow_blank=True, default='')


class MaterialRequestInputSerializer(serializers.Serializer):
    factory = serializers.PrimaryKeyRelatedField(queryset=Factory.objects.filter(is_active=True))
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all(), required=False, allow_null=True)
    required_by = serializers.DateField(required=False, allow_null=True)
    production_order = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    cost_centre = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    lines = RequestLineInputSerializer(many=True, min_length=1)

    def validate(self, attrs):
        dept = attrs.get('department')
        if dept and dept.factory_id != attrs['factory'].pk:
            raise serializers.ValidationError({'department': 'Department belongs to another factory.'})
        return attrs


class MaterialIssueLineSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)
    material_code = serializers.CharField(source='material.code', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    returned_quantity = serializers.SerializerMethodField()

    class Meta:
        model = MaterialIssueLine
        fields = ['id', 'package', 'package_code', 'material', 'material_code', 'batch_number', 'issued_quantity',
                  'unit', 'unit_symbol', 'qc_status_at_issue', 'fifo_compliant', 'request_line', 'returned_quantity',
                  'notes']
        read_only_fields = fields

    def get_returned_quantity(self, obj) -> str:
        return str(sum((r.returned_quantity for r in obj.returns.all()), Decimal('0')))


class MaterialIssueSerializer(serializers.ModelSerializer):
    issue_lines = MaterialIssueLineSerializer(many=True, read_only=True)
    factory_name = serializers.CharField(source='factory.name', read_only=True)
    department_name = serializers.CharField(source='department.name', read_only=True, default=None)
    request_number = serializers.CharField(source='material_request.request_number', read_only=True, default=None)
    issued_by_name = serializers.CharField(source='issued_by.display_name', read_only=True)
    received_by_display = serializers.SerializerMethodField()
    acknowledged_by_name = serializers.CharField(source='acknowledged_by.display_name', read_only=True, default=None)
    override_approved_by_name = serializers.CharField(source='fifo_override_approved_by.display_name', read_only=True, default=None)

    class Meta:
        model = MaterialIssue
        fields = ['id', 'issue_number', 'material_request', 'request_number', 'factory', 'factory_name',
                  'department', 'department_name', 'production_order', 'cost_centre', 'reference',
                  'issued_by', 'issued_by_name', 'received_by', 'received_by_name', 'received_by_display',
                  'issue_datetime', 'status', 'acknowledged_by', 'acknowledged_by_name', 'acknowledged_at', 'notes',
                  'fifo_override', 'fifo_override_reason', 'fifo_override_approved_by', 'override_approved_by_name',
                  'issue_lines', 'created_at']
        read_only_fields = fields

    def get_received_by_display(self, obj) -> str:
        return obj.received_by.display_name if obj.received_by_id else obj.received_by_name


class IssueLineInputSerializer(serializers.Serializer):
    package = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all())
    quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))


class MaterialIssueInputSerializer(serializers.Serializer):
    material_request = serializers.PrimaryKeyRelatedField(queryset=MaterialRequest.objects.all(), required=False, allow_null=True)
    factory = serializers.PrimaryKeyRelatedField(queryset=Factory.objects.filter(is_active=True), required=False)
    department = serializers.PrimaryKeyRelatedField(queryset=Department.objects.all(), required=False, allow_null=True)
    production_order = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    cost_centre = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    reference = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    received_by = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True), required=False, allow_null=True)
    received_by_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    notes = serializers.CharField(required=False, allow_blank=True, default='')
    override_reason = serializers.CharField(required=False, allow_blank=True, default='')
    lines = IssueLineInputSerializer(many=True, min_length=1)

    def validate(self, attrs):
        if not attrs.get('material_request') and not attrs.get('factory'):
            raise serializers.ValidationError({'factory': 'A factory or a material request is required.'})
        if not attrs.get('received_by') and not attrs.get('received_by_name'):
            raise serializers.ValidationError({'received_by_name': 'Record who received the material.'})
        return attrs


class MaterialReturnSerializer(serializers.ModelSerializer):
    issue_number = serializers.CharField(source='issue_line.issue.issue_number', read_only=True)
    original_package = serializers.CharField(source='issue_line.package.package_id', read_only=True)
    return_package_code = serializers.CharField(source='return_package.package_id', read_only=True, default=None)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)

    class Meta:
        model = MaterialReturn
        fields = ['id', 'return_number', 'issue_line', 'issue_number', 'original_package', 'return_package',
                  'return_package_code', 'returned_quantity', 'unit', 'unit_symbol', 'reason', 'returned_by_name',
                  'received_by', 'requires_qc', 'notes', 'created_at']
        read_only_fields = fields


class MaterialReturnInputSerializer(serializers.Serializer):
    issue_line = serializers.PrimaryKeyRelatedField(queryset=MaterialIssueLine.objects.all())
    returned_quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))
    reason = serializers.CharField()
    returned_by_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    requires_qc = serializers.BooleanField(default=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class DisposalRecordSerializer(serializers.ModelSerializer):
    package_code = serializers.CharField(source='package.package_id', read_only=True)
    unit_symbol = serializers.CharField(source='unit.symbol', read_only=True)
    authorized_by_name = serializers.CharField(source='authorized_by.display_name', read_only=True, default=None)

    class Meta:
        model = DisposalRecord
        fields = ['id', 'disposal_number', 'disposal_type', 'package', 'package_code', 'quantity', 'unit',
                  'unit_symbol', 'reason', 'reference', 'disposal_date', 'authorized_by', 'authorized_by_name',
                  'notes', 'created_at']
        read_only_fields = fields


class DisposalInputSerializer(serializers.Serializer):
    package = serializers.PrimaryKeyRelatedField(queryset=Package.objects.all())
    disposal_type = serializers.ChoiceField(choices=DisposalType.choices, default=DisposalType.SCRAP)
    quantity = serializers.DecimalField(max_digits=15, decimal_places=4, min_value=Decimal('0.0001'))
    reason = serializers.CharField()
    reference = serializers.CharField(max_length=200, required=False, allow_blank=True, default='')
    disposal_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True, default='')
