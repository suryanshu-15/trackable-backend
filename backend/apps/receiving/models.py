"""
Receiving models: GoodsReceipt, InvoiceLine, ReceiptDocument, ReceiptLineVerification.
"""
import os
import uuid
from django.db import models
from django.core.exceptions import ValidationError
from django.conf import settings
from django_fsm import FSMField, transition

from apps.core.mixins import TimeStampedModel, AuditedModel, UUIDModel
from apps.core.utils import generate_receipt_number
from apps.master.models import Supplier, Material, UnitOfMeasure


# ---------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------

class ReceiptStatus(models.TextChoices):
    DRAFT = 'DRAFT', 'Draft'
    GATE_REGISTERED = 'GATE_REGISTERED', 'Gate Registered'
    AWAITING_UNLOADING = 'AWAITING_UNLOADING', 'Awaiting Unloading'
    UNLOADING_IN_PROGRESS = 'UNLOADING_IN_PROGRESS', 'Unloading In Progress'
    VERIFICATION_PENDING = 'VERIFICATION_PENDING', 'Verification Pending'
    EXCEPTION = 'EXCEPTION', 'Exception'
    UNLOADED = 'UNLOADED', 'Unloaded'
    PUTAWAY_IN_PROGRESS = 'PUTAWAY_IN_PROGRESS', 'Put-away In Progress'
    STORED = 'STORED', 'Stored'
    CLOSED = 'CLOSED', 'Closed'
    CANCELLED = 'CANCELLED', 'Cancelled'


class WeighingMethod(models.TextChoices):
    WEIGHBRIDGE = 'WEIGHBRIDGE', 'Weighbridge'
    PLATFORM_SCALE = 'PLATFORM_SCALE', 'Platform Scale'
    VENDOR_DECLARED = 'VENDOR_DECLARED', 'Vendor Declared'
    OTHER = 'OTHER', 'Other'


class DocumentType(models.TextChoices):
    INVOICE = 'INVOICE', 'Invoice'
    DELIVERY_CHALLAN = 'DELIVERY_CHALLAN', 'Delivery Challan'
    WEIGHMENT_SLIP = 'WEIGHMENT_SLIP', 'Weighment Slip'
    QC_REPORT = 'QC_REPORT', 'QC / COA Report'
    PACKING_LIST = 'PACKING_LIST', 'Packing List'
    OTHER = 'OTHER', 'Other'


# ---------------------------------------------------------------------------
# GoodsReceipt
# ---------------------------------------------------------------------------

def _receipt_doc_upload_path(instance, filename):
    # Never reuse the client filename on disk; it is kept in original_filename.
    ext = os.path.splitext(filename)[1].lower()
    return f"receipts/{instance.receipt.receipt_number}/{instance.document_type}-{uuid.uuid4().hex}{ext}"


class GoodsReceipt(UUIDModel, AuditedModel):
    """
    Stage 1 — Gate entry for an incoming truck.
    One receipt can have many invoice lines.
    """
    receipt_number = models.CharField(
        max_length=50, unique=True, db_index=True,
        help_text='System-generated unique inward reference.',
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT,
        related_name='receipts', db_index=True,
    )
    # Invoice details
    invoice_number = models.CharField(max_length=100, db_index=True)
    invoice_date = models.DateField()
    # Truck
    vehicle_number = models.CharField(max_length=50, db_index=True)
    driver_name = models.CharField(max_length=100, blank=True)
    # Arrival
    arrival_datetime = models.DateTimeField(db_index=True)
    # Weighing
    gross_weight = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    tare_weight = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    net_weight = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    weight_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.PROTECT, related_name='+',
    )
    weighing_method = models.CharField(
        max_length=30, choices=WeighingMethod.choices,
        default=WeighingMethod.VENDOR_DECLARED,
    )
    weighbridge_reference = models.CharField(max_length=100, blank=True)
    # References
    purchase_order_number = models.CharField(max_length=100, blank=True, db_index=True)
    delivery_challan_number = models.CharField(max_length=100, blank=True)
    # State machine
    status = FSMField(default=ReceiptStatus.DRAFT, choices=ReceiptStatus.choices, protected=True, db_index=True)
    # Notes
    gate_notes = models.TextField(blank=True)
    # Timestamps for key events
    gate_registered_at = models.DateTimeField(null=True, blank=True)
    unloading_started_at = models.DateTimeField(null=True, blank=True)
    unloading_completed_at = models.DateTimeField(null=True, blank=True)
    stored_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'receiving_goods_receipt'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['supplier', 'invoice_number', 'invoice_date']),
            models.Index(fields=['status', 'arrival_datetime']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(gross_weight__isnull=True) | models.Q(tare_weight__isnull=True)
                | models.Q(gross_weight__gte=models.F('tare_weight')),
                name='receipt_gross_gte_tare',
            ),
            models.CheckConstraint(
                check=models.Q(tare_weight__isnull=True) | models.Q(tare_weight__gte=0),
                name='receipt_tare_non_negative',
            ),
        ]

    def __str__(self):
        return f"{self.receipt_number} — {self.supplier.name}"

    def save(self, *args, **kwargs):
        if not self.receipt_number:
            self.receipt_number = generate_receipt_number()
        # Net weight is always derived when both readings exist
        if self.gross_weight is not None and self.tare_weight is not None:
            self.net_weight = self.gross_weight - self.tare_weight
        super().save(*args, **kwargs)

    def clean(self):
        if self.gross_weight is not None and self.tare_weight is not None:
            if self.gross_weight < self.tare_weight:
                raise ValidationError(
                    {'tare_weight': 'Tare weight cannot exceed gross weight. Use correction workflow if needed.'}
                )
        if self.net_weight is not None and self.net_weight < 0:
            raise ValidationError({'net_weight': 'Net weight cannot be negative.'})

    # --- FSM transitions ---

    @transition(field=status, source=ReceiptStatus.DRAFT, target=ReceiptStatus.GATE_REGISTERED)
    def register_at_gate(self):
        from django.utils import timezone
        self.gate_registered_at = timezone.now()

    @transition(field=status, source=ReceiptStatus.GATE_REGISTERED, target=ReceiptStatus.AWAITING_UNLOADING)
    def submit_for_unloading(self):
        pass

    @transition(field=status, source=ReceiptStatus.AWAITING_UNLOADING, target=ReceiptStatus.UNLOADING_IN_PROGRESS)
    def start_unloading(self):
        from django.utils import timezone
        self.unloading_started_at = timezone.now()

    @transition(
        field=status,
        source=[ReceiptStatus.UNLOADING_IN_PROGRESS, ReceiptStatus.EXCEPTION],
        target=ReceiptStatus.VERIFICATION_PENDING,
    )
    def mark_verification_pending(self):
        pass

    @transition(field=status, source=ReceiptStatus.UNLOADING_IN_PROGRESS, target=ReceiptStatus.EXCEPTION)
    def flag_exception(self):
        pass

    @transition(field=status, source=ReceiptStatus.EXCEPTION, target=ReceiptStatus.UNLOADING_IN_PROGRESS)
    def resume_unloading(self):
        """System-only: every discrepancy was cleared by re-verification."""

    @transition(
        field=status,
        source=[ReceiptStatus.VERIFICATION_PENDING, ReceiptStatus.EXCEPTION],
        target=ReceiptStatus.UNLOADED,
    )
    def mark_unloaded(self):
        from django.utils import timezone
        self.unloading_completed_at = timezone.now()

    @transition(field=status, source=ReceiptStatus.UNLOADED, target=ReceiptStatus.PUTAWAY_IN_PROGRESS)
    def start_putaway(self):
        pass

    @transition(field=status, source=ReceiptStatus.PUTAWAY_IN_PROGRESS, target=ReceiptStatus.STORED)
    def mark_stored(self):
        from django.utils import timezone
        self.stored_at = timezone.now()

    @transition(field=status, source=ReceiptStatus.STORED, target=ReceiptStatus.CLOSED)
    def close(self):
        from django.utils import timezone
        self.closed_at = timezone.now()

    @transition(
        field=status,
        source=[ReceiptStatus.DRAFT, ReceiptStatus.GATE_REGISTERED],
        target=ReceiptStatus.CANCELLED,
    )
    def cancel(self):
        pass


class ReceiptDocument(UUIDModel, TimeStampedModel):
    """Uploaded documents attached to a goods receipt."""
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name='documents')
    document_type = models.CharField(max_length=30, choices=DocumentType.choices)
    file = models.FileField(upload_to=_receipt_doc_upload_path)
    original_filename = models.CharField(max_length=255)
    file_size_bytes = models.PositiveIntegerField()
    mime_type = models.CharField(max_length=100, blank=True)
    notes = models.CharField(max_length=500, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        db_table = 'receiving_receipt_document'

    def __str__(self):
        return f"{self.receipt.receipt_number} — {self.document_type}"


# ---------------------------------------------------------------------------
# InvoiceLine
# ---------------------------------------------------------------------------

class InvoiceLine(UUIDModel, AuditedModel):
    """
    Stage 2 — One line on the invoice inside a GoodsReceipt.
    Keeps declared invoice values separate from verified values.
    """
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.PROTECT, related_name='invoice_lines')
    line_number = models.PositiveSmallIntegerField()
    # Material
    material = models.ForeignKey(
        Material, null=True, blank=True,
        on_delete=models.PROTECT, related_name='invoice_lines',
    )
    material_code_declared = models.CharField(max_length=100, blank=True)
    material_name_declared = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    grade_or_specification = models.CharField(max_length=200, blank=True)
    # Declared quantity
    declared_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    declared_unit = models.ForeignKey(
        UnitOfMeasure, on_delete=models.PROTECT, related_name='+',
    )
    declared_weight = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    declared_weight_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.PROTECT, related_name='+',
    )
    # Lot / batch info
    batch_number = models.CharField(max_length=200, blank=True, db_index=True)
    manufacturing_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    retest_date = models.DateField(null=True, blank=True)
    # Quality
    required_tests = models.ManyToManyField('master.LabTestDefinition', blank=True, related_name='+')
    quality_specification = models.TextField(blank=True)
    # Remarks
    remarks = models.TextField(blank=True)

    class Meta:
        db_table = 'receiving_invoice_line'
        unique_together = [('receipt', 'line_number')]
        ordering = ['receipt', 'line_number']
        constraints = [
            models.CheckConstraint(check=models.Q(declared_quantity__gt=0), name='invoice_line_qty_positive'),
        ]

    def __str__(self):
        return f"{self.receipt.receipt_number} / Line {self.line_number} — {self.material_name_declared}"


# ---------------------------------------------------------------------------
# ReceiptLineVerification
# ---------------------------------------------------------------------------

class DiscrepancyStatus(models.TextChoices):
    NONE = 'NONE', 'No Discrepancy'
    UNIT_MISMATCH = 'UNIT_MISMATCH', 'Unit differs and no conversion is configured'
    QUANTITY_SHORT = 'QUANTITY_SHORT', 'Short Quantity'
    QUANTITY_EXCESS = 'QUANTITY_EXCESS', 'Excess Quantity'
    DAMAGED = 'DAMAGED', 'Damaged'
    DESCRIPTION_MISMATCH = 'DESCRIPTION_MISMATCH', 'Description Mismatch'
    UNIDENTIFIED = 'UNIDENTIFIED', 'Unidentified Material'
    REJECTED = 'REJECTED', 'Line Rejected'
    MULTIPLE = 'MULTIPLE', 'Multiple Discrepancies'


class VerificationStatus(models.TextChoices):
    PENDING = 'PENDING', 'Pending'
    VERIFIED = 'VERIFIED', 'Verified (No Discrepancy)'
    DISCREPANT = 'DISCREPANT', 'Discrepant — Flagged for Review'
    EXCEPTION_ACCEPTED = 'EXCEPTION_ACCEPTED', 'Exception Accepted by Authority'


class ReceiptLineVerification(UUIDModel, AuditedModel):
    """
    Stage 3 — Physical unloading verification per invoice line.
    Does NOT overwrite declared invoice values.
    """
    invoice_line = models.OneToOneField(InvoiceLine, on_delete=models.PROTECT, related_name='verification')
    # Actual received
    received_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    received_unit = models.ForeignKey(
        UnitOfMeasure, null=True, blank=True,
        on_delete=models.PROTECT, related_name='+',
    )
    accepted_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    short_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    excess_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    damaged_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    mismatched_quantity = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)
    # Material master the physical goods were identified as, when the invoice line
    # had no master material (the declared invoice values are never overwritten)
    identified_material = models.ForeignKey(
        Material, null=True, blank=True,
        on_delete=models.PROTECT, related_name='+',
    )
    # Observed differences
    observed_description = models.TextField(blank=True)
    observed_grade = models.CharField(max_length=200, blank=True)
    # Timing
    unloading_start = models.DateTimeField(null=True, blank=True)
    unloading_end = models.DateTimeField(null=True, blank=True)
    # Status
    verification_status = models.CharField(
        max_length=30,
        choices=VerificationStatus.choices,
        default=VerificationStatus.PENDING,
        db_index=True,
    )
    discrepancy_status = models.CharField(
        max_length=30,
        choices=DiscrepancyStatus.choices,
        default=DiscrepancyStatus.NONE,
    )
    discrepancy_notes = models.TextField(blank=True)
    # Exception authorization
    exception_authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    exception_reason = models.TextField(blank=True)
    # Worker
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    verification_notes = models.TextField(blank=True)

    class Meta:
        db_table = 'receiving_line_verification'

    def __str__(self):
        return f"Verification for {self.invoice_line}"
