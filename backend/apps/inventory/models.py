"""
Inventory models: Package, PackageLabelEvent, InventoryMovement,
StockAdjustment, MaterialRequest, MaterialIssue, MaterialReturn, DisposalRecord.
"""
import uuid
from decimal import Decimal
from django.db import models
from django.core.validators import MinValueValidator
from django.conf import settings
from django.utils import timezone
from django_fsm import FSMField, transition

from apps.core.mixins import TimeStampedModel, AuditedModel, UUIDModel
from apps.core.utils import generate_package_token, generate_issue_number, generate_reference
from apps.master.models import Material, UnitOfMeasure, StorageLocation
from apps.receiving.models import InvoiceLine


# ---------------------------------------------------------------------------
# Package status dimensions
# ---------------------------------------------------------------------------
# Physical lifecycle, quality status, and availability are modelled separately
# (spec "critical design reminder"). `status` is the physical/inventory lifecycle
# only; `qc_status` is the laboratory outcome; holds and recalls are flags; and
# availability for issue is always *derived* (see PackageQuerySet.available),
# never stored, so it cannot go stale when e.g. an expiry date passes.

class PackageStatus(models.TextChoices):
    CREATED = 'CREATED', 'Created (label not printed)'
    AWAITING_PUTAWAY = 'AWAITING_PUTAWAY', 'Awaiting Put-away'
    STORED = 'STORED', 'Stored'
    ISSUED = 'ISSUED', 'Fully Issued'
    REPACKED = 'REPACKED', 'Repacked into child packages'
    DISPOSED = 'DISPOSED', 'Disposed'
    SUPPLIER_RETURNED = 'SUPPLIER_RETURNED', 'Returned to Supplier'


class QCStatus(models.TextChoices):
    NOT_REQUIRED = 'NOT_REQUIRED', 'QC Not Required'
    PENDING_SAMPLING = 'PENDING_SAMPLING', 'Pending Sampling'
    SAMPLED = 'SAMPLED', 'Sampled'
    TESTING = 'TESTING', 'Testing'
    PASSED = 'PASSED', 'Passed'
    FAILED = 'FAILED', 'Failed'
    ON_HOLD = 'ON_HOLD', 'On Hold'
    RETEST_REQUIRED = 'RETEST_REQUIRED', 'Retest Required'


# QC outcomes that permit production issue
QC_RELEASED_STATUSES = (QCStatus.PASSED, QCStatus.NOT_REQUIRED)
# QC statuses that still require the package to sit in quarantine
QC_PENDING_STATUSES = (
    QCStatus.PENDING_SAMPLING, QCStatus.SAMPLED, QCStatus.TESTING, QCStatus.RETEST_REQUIRED,
)
# Lifecycle states in which a package physically holds stock in the warehouse
IN_WAREHOUSE_STATUSES = (PackageStatus.CREATED, PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED)


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

def _generate_package_id():
    """Human-readable package ID: PKG-<10 hex>. Uniqueness enforced by the DB; callers retry on collision."""
    return f"PKG-{uuid.uuid4().hex[:10].upper()}"


class PackageQuerySet(models.QuerySet):
    def in_warehouse(self):
        return self.filter(status__in=IN_WAREHOUSE_STATUSES, current_quantity__gt=0)

    def available(self, on_date=None):
        """Eligible for production issue right now. The single source of truth for availability."""
        today = on_date or timezone.localdate()
        return self.filter(
            status=PackageStatus.STORED,
            qc_status__in=QC_RELEASED_STATUSES,
            is_on_hold=False,
            is_recalled=False,
            current_quantity__gt=0,
        ).filter(
            models.Q(expiry_date__isnull=True) | models.Q(expiry_date__gt=today),
        ).filter(
            models.Q(retest_date__isnull=True) | models.Q(retest_date__gte=today),
        )


class Package(UUIDModel, AuditedModel):
    """
    Stage 4 — One physical bag/drum/pallet with a unique, immutable ID.
    Quantity changes only through the movement ledger (services.apply_quantity_change).
    """
    package_id = models.CharField(
        max_length=50, unique=True, db_index=True, editable=False,
    )
    scan_token = models.CharField(
        max_length=64, unique=True, db_index=True, editable=False,
        help_text='Opaque token stored in QR code — resolves to this record server-side.',
    )
    # Lineage
    invoice_line = models.ForeignKey(
        InvoiceLine, on_delete=models.PROTECT, related_name='packages',
    )
    parent_package = models.ForeignKey(
        'self', null=True, blank=True,
        on_delete=models.PROTECT, related_name='child_packages',
        help_text='Set when a package is split / repacked from another.',
    )
    split_reason = models.CharField(max_length=500, blank=True)
    # Material
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='packages')
    supplier = models.ForeignKey('master.Supplier', on_delete=models.PROTECT, related_name='packages')
    # Quantity — original is immutable; current is a cached balance that must equal the ledger sum
    original_quantity = models.DecimalField(
        max_digits=15, decimal_places=4,
        validators=[MinValueValidator(Decimal('0.0001'))],
    )
    current_quantity = models.DecimalField(
        max_digits=15, decimal_places=4,
        validators=[MinValueValidator(Decimal('0'))],
    )
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    # Lot info
    batch_number = models.CharField(max_length=200, blank=True, db_index=True)
    manufacturing_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    retest_date = models.DateField(null=True, blank=True, db_index=True)
    grade = models.CharField(max_length=200, blank=True)
    # FIFO timestamp: when the material was received (truck arrival), inherited by child packages
    received_at = models.DateTimeField(db_index=True)
    # Dimension 1 — physical lifecycle
    status = FSMField(default=PackageStatus.CREATED, choices=PackageStatus.choices, protected=True, db_index=True)
    current_location = models.ForeignKey(
        StorageLocation, null=True, blank=True,
        on_delete=models.PROTECT, related_name='packages',
    )
    # Dimension 2 — quality
    qc_status = models.CharField(
        max_length=20, choices=QCStatus.choices,
        default=QCStatus.PENDING_SAMPLING, db_index=True,
    )
    # Dimension 3 — blocking flags (maintained by laboratory hold / recall workflows)
    is_on_hold = models.BooleanField(default=False, db_index=True)
    is_recalled = models.BooleanField(default=False, db_index=True)
    notes = models.TextField(blank=True)
    # Label
    label_printed_at = models.DateTimeField(null=True, blank=True)
    label_print_count = models.PositiveIntegerField(default=0)

    objects = PackageQuerySet.as_manager()

    class Meta:
        db_table = 'inventory_package'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['material', 'status', 'qc_status']),
            models.Index(fields=['batch_number', 'material']),
            models.Index(fields=['expiry_date', 'material']),
            models.Index(fields=['current_location', 'status']),
            models.Index(fields=['material', 'received_at']),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(original_quantity__gt=0), name='package_original_qty_positive'),
            models.CheckConstraint(check=models.Q(current_quantity__gte=0), name='package_current_qty_non_negative'),
        ]

    def __str__(self):
        return f"{self.package_id} ({self.material.code})"

    def save(self, *args, **kwargs):
        if not self.package_id:
            self.package_id = _generate_package_id()
        if not self.scan_token:
            self.scan_token = generate_package_token()
        super().save(*args, **kwargs)

    def blocking_reasons(self, on_date=None):
        """Why this package cannot be issued now (empty list = available). Mirrors PackageQuerySet.available."""
        today = on_date or timezone.localdate()
        reasons = []
        if self.status != PackageStatus.STORED:
            reasons.append(f'status is {self.get_status_display()}')
        if self.qc_status not in QC_RELEASED_STATUSES:
            reasons.append(f'QC status is {self.get_qc_status_display()}')
        if self.is_on_hold:
            reasons.append('on quality hold')
        if self.is_recalled:
            reasons.append('recalled')
        if self.current_quantity <= 0:
            reasons.append('no remaining quantity')
        if self.expiry_date and self.expiry_date <= today:
            reasons.append(f'expired on {self.expiry_date}')
        if self.retest_date and self.retest_date < today:
            reasons.append(f'retest overdue since {self.retest_date}')
        return reasons

    @property
    def is_available(self):
        return not self.blocking_reasons()

    @property
    def requires_quarantine(self):
        return self.qc_status in QC_PENDING_STATUSES or self.qc_status in (QCStatus.FAILED, QCStatus.ON_HOLD)

    # --- Lifecycle transitions (physical only; never touch quality) ---

    @transition(field=status, source=PackageStatus.CREATED, target=PackageStatus.AWAITING_PUTAWAY)
    def mark_awaiting_putaway(self):
        pass

    @transition(field=status, source=PackageStatus.AWAITING_PUTAWAY, target=PackageStatus.STORED)
    def store(self):
        pass

    @transition(field=status, source=PackageStatus.STORED, target=PackageStatus.ISSUED)
    def mark_issued(self):
        pass

    @transition(field=status, source=PackageStatus.ISSUED, target=PackageStatus.STORED)
    def restock(self):
        pass

    @transition(field=status, source=[PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED], target=PackageStatus.REPACKED)
    def mark_repacked(self):
        pass

    @transition(field=status, source=list(IN_WAREHOUSE_STATUSES), target=PackageStatus.DISPOSED)
    def dispose(self):
        pass

    @transition(field=status, source=list(IN_WAREHOUSE_STATUSES), target=PackageStatus.SUPPLIER_RETURNED)
    def return_to_supplier(self):
        pass


class PackageLabelEvent(UUIDModel, TimeStampedModel):
    """Immutable record of every label print or reprint."""
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='label_events')
    event_type = models.CharField(
        max_length=20,
        choices=[('PRINT', 'Initial Print'), ('REPRINT', 'Reprint'), ('RELEASE_TAG', 'Green release tag')],
    )
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    printer_reference = models.CharField(max_length=200, blank=True)
    reprint_reason = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = 'inventory_package_label_event'
        ordering = ['created_at']

    def __str__(self):
        return f"{self.event_type} — {self.package.package_id}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("PackageLabelEvent records are immutable.")
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Inventory Movement Ledger
# ---------------------------------------------------------------------------

class MovementReason(models.TextChoices):
    RECEIPT = 'RECEIPT', 'Received (package created)'
    PUTAWAY = 'PUTAWAY', 'Initial Put-away'
    TRANSFER = 'TRANSFER', 'Location Transfer'
    QC_MOVE = 'QC_MOVE', 'QC / Quarantine Move'
    ISSUE = 'ISSUE', 'Issue to Factory'
    RETURN = 'RETURN', 'Return from Factory'
    ADJUSTMENT = 'ADJUSTMENT', 'Stock Adjustment'
    DISPOSAL = 'DISPOSAL', 'Disposal'
    SUPPLIER_RETURN = 'SUPPLIER_RETURN', 'Return to Supplier'
    SPLIT_OUT = 'SPLIT_OUT', 'Repack — quantity moved out to child'
    SPLIT_IN = 'SPLIT_IN', 'Repack — quantity received from parent'
    SAMPLE = 'SAMPLE', 'Laboratory sample drawn'
    OTHER = 'OTHER', 'Other'


class InventoryMovement(UUIDModel, TimeStampedModel):
    """
    Immutable append-only movement ledger.

    `quantity_delta` is the signed change to the package balance (0 for pure location
    moves). For every package, SUM(quantity_delta) must equal Package.current_quantity,
    and the last location-changing movement's to_location must equal current_location.
    """
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='movements')
    from_location = models.ForeignKey(
        StorageLocation, null=True, blank=True,
        on_delete=models.PROTECT, related_name='movements_from',
    )
    to_location = models.ForeignKey(
        StorageLocation, null=True, blank=True,
        on_delete=models.PROTECT, related_name='movements_to',
    )
    # Quantity physically involved (always ≥ 0)
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    # Signed change to the package balance
    quantity_delta = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'))
    balance_after = models.DecimalField(max_digits=15, decimal_places=4)
    location_changed = models.BooleanField(default=False)
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    reason = models.CharField(max_length=30, choices=MovementReason.choices, db_index=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    notes = models.TextField(blank=True)
    reference_id = models.CharField(max_length=200, blank=True, db_index=True)

    class Meta:
        db_table = 'inventory_movement'
        ordering = ['-created_at']
        indexes = [models.Index(fields=['package', 'created_at'])]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gte=0), name='movement_quantity_non_negative'),
            models.CheckConstraint(check=models.Q(balance_after__gte=0), name='movement_balance_non_negative'),
        ]

    def __str__(self):
        return f"{self.package.package_id} {self.reason} {self.quantity_delta:+}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValueError("InventoryMovement records are immutable.")
        super().save(*args, **kwargs)


class StockAdjustment(UUIDModel, AuditedModel):
    """
    Controlled quantity correction: requested by one user, approved by another.
    INCREASE / DECREASE carry a positive amount; CORRECTION carries the physically counted quantity.
    """

    ADJUSTMENT_TYPE_CHOICES = [
        ('INCREASE', 'Increase'),
        ('DECREASE', 'Decrease'),
        ('CORRECTION', 'Physical Count Correction'),
    ]
    STATUS_CHOICES = [
        ('PENDING', 'Pending Approval'),
        ('APPROVED', 'Approved'),
        ('REJECTED', 'Rejected'),
    ]

    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='adjustments')
    adjustment_type = models.CharField(max_length=20, choices=ADJUSTMENT_TYPE_CHOICES)
    quantity_change = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    reason = models.TextField()
    reference = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING', db_index=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_notes = models.TextField(blank=True)
    applied_delta = models.DecimalField(max_digits=15, decimal_places=4, null=True, blank=True)

    class Meta:
        db_table = 'inventory_stock_adjustment'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(quantity_change__gte=0), name='adjustment_quantity_non_negative'),
        ]

    def __str__(self):
        return f"{self.adjustment_type} {self.quantity_change} on {self.package.package_id}"


# ---------------------------------------------------------------------------
# Material Request, Reservation & Issue (Phase 6)
# ---------------------------------------------------------------------------

class RequestStatus(models.TextChoices):
    SUBMITTED = 'SUBMITTED', 'Submitted'
    ALLOCATED = 'ALLOCATED', 'Stock allocated'
    PARTIALLY_ISSUED = 'PARTIALLY_ISSUED', 'Partially issued'
    FULFILLED = 'FULFILLED', 'Fulfilled'
    CLOSED = 'CLOSED', 'Closed short'
    CANCELLED = 'CANCELLED', 'Cancelled'


OPEN_REQUEST_STATUSES = (RequestStatus.SUBMITTED, RequestStatus.ALLOCATED, RequestStatus.PARTIALLY_ISSUED)


class IssueStatus(models.TextChoices):
    ISSUED = 'ISSUED', 'Issued'
    ACKNOWLEDGED = 'ACKNOWLEDGED', 'Receipt acknowledged'


class MaterialRequest(UUIDModel, AuditedModel):
    """A factory's request for materials (the authorisation for an issue)."""
    request_number = models.CharField(max_length=50, unique=True, db_index=True)
    factory = models.ForeignKey('master.Factory', on_delete=models.PROTECT, related_name='requests')
    department = models.ForeignKey('master.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='material_requests',
    )
    required_by = models.DateField(null=True, blank=True)
    production_order = models.CharField(max_length=200, blank=True, db_index=True)
    cost_centre = models.CharField(max_length=100, blank=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=RequestStatus.choices, default=RequestStatus.SUBMITTED, db_index=True)
    closed_reason = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_material_request'
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.request_number:
            self.request_number = generate_reference('REQ', 6)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.request_number


class MaterialRequestLine(UUIDModel, TimeStampedModel):
    request = models.ForeignKey(MaterialRequest, on_delete=models.PROTECT, related_name='lines')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='+')
    requested_quantity = models.DecimalField(max_digits=15, decimal_places=4, validators=[MinValueValidator(Decimal('0.0001'))])
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    # Cached running total; reconciles with SUM(MaterialIssueLine.issued_quantity)
    issued_quantity = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal('0'))
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = 'inventory_material_request_line'
        unique_together = [('request', 'material', 'unit')]
        constraints = [
            models.CheckConstraint(check=models.Q(requested_quantity__gt=0), name='request_line_qty_positive'),
        ]

    @property
    def remaining_quantity(self):
        return max(self.requested_quantity - self.issued_quantity, Decimal('0'))

    def __str__(self):
        return f"{self.request.request_number} / {self.material.code}"


class StockReservation(UUIDModel, TimeStampedModel):
    """Quantity of a package set aside for a request line; unavailable to anyone else while active."""
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='reservations')
    request_line = models.ForeignKey(MaterialRequestLine, on_delete=models.PROTECT, related_name='reservations')
    reserved_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    is_active = models.BooleanField(default=True, db_index=True)
    reserved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    released_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'inventory_stock_reservation'
        constraints = [
            models.CheckConstraint(check=models.Q(reserved_quantity__gte=0), name='reservation_qty_non_negative'),
        ]

    def __str__(self):
        return f"Reservation {self.package.package_id} for {self.request_line}"


class MaterialIssue(UUIDModel, AuditedModel):
    """Issue transaction. FIFO/FEFO is enforced; any override is explicit, authorised and audited."""
    issue_number = models.CharField(max_length=50, unique=True, db_index=True)
    material_request = models.ForeignKey(
        MaterialRequest, null=True, blank=True,
        on_delete=models.PROTECT, related_name='issues',
    )
    factory = models.ForeignKey('master.Factory', on_delete=models.PROTECT, related_name='+')
    department = models.ForeignKey('master.Department', null=True, blank=True, on_delete=models.PROTECT, related_name='+')
    production_order = models.CharField(max_length=200, blank=True, db_index=True)
    cost_centre = models.CharField(max_length=100, blank=True)
    reference = models.CharField(max_length=200, blank=True)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='issued_by',
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    received_by_name = models.CharField(max_length=200, blank=True)
    issue_datetime = models.DateTimeField(db_index=True)
    status = models.CharField(max_length=20, choices=IssueStatus.choices, default=IssueStatus.ISSUED, db_index=True)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='+',
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    notes = models.TextField(blank=True)
    # FIFO/FEFO override
    fifo_override = models.BooleanField(default=False, db_index=True)
    fifo_override_reason = models.TextField(blank=True)
    fifo_override_approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )

    class Meta:
        db_table = 'inventory_material_issue'
        ordering = ['-issue_datetime']

    def save(self, *args, **kwargs):
        if not self.issue_number:
            self.issue_number = generate_issue_number()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.issue_number


class MaterialIssueLine(UUIDModel, TimeStampedModel):
    """One package's contribution to an issue."""
    issue = models.ForeignKey(MaterialIssue, on_delete=models.PROTECT, related_name='issue_lines')
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='issue_lines')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='+')
    request_line = models.ForeignKey(MaterialRequestLine, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name='issue_lines')
    issued_quantity = models.DecimalField(
        max_digits=15, decimal_places=4,
        validators=[MinValueValidator(Decimal('0.0001'))],
    )
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    # Snapshot of the quality status at issue time
    qc_status_at_issue = models.CharField(max_length=20, choices=QCStatus.choices)
    batch_number = models.CharField(max_length=200, blank=True)
    fifo_compliant = models.BooleanField(default=True)
    notes = models.CharField(max_length=500, blank=True)

    class Meta:
        db_table = 'inventory_material_issue_line'
        unique_together = [('issue', 'package')]
        constraints = [
            models.CheckConstraint(check=models.Q(issued_quantity__gt=0), name='issue_line_qty_positive'),
        ]

    def __str__(self):
        return f"{self.issue.issue_number} / {self.package.package_id}"


class MaterialReturn(UUIDModel, AuditedModel):
    """
    Unused material returned from the factory. The quantity goes into a NEW child package
    (so it is inspected separately and never mixes with untouched stock) and is blocked until cleared.
    """
    return_number = models.CharField(max_length=50, unique=True, db_index=True)
    issue_line = models.ForeignKey(MaterialIssueLine, on_delete=models.PROTECT, related_name='returns')
    return_package = models.OneToOneField(Package, null=True, blank=True, on_delete=models.PROTECT,
                                          related_name='return_record')
    returned_quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    reason = models.TextField()
    returned_by_name = models.CharField(max_length=200, blank=True)
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    requires_qc = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_material_return'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(returned_quantity__gt=0), name='return_qty_positive'),
        ]

    def save(self, *args, **kwargs):
        if not self.return_number:
            self.return_number = generate_reference('RET', 6)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.return_number


class DisposalType(models.TextChoices):
    SCRAP = 'SCRAP', 'Disposal / scrapping'
    SUPPLIER_RETURN = 'SUPPLIER_RETURN', 'Return to supplier'


class DisposalRecord(UUIDModel, AuditedModel):
    disposal_number = models.CharField(max_length=50, unique=True, db_index=True)
    disposal_type = models.CharField(max_length=20, choices=DisposalType.choices, default=DisposalType.SCRAP, db_index=True)
    package = models.ForeignKey(Package, on_delete=models.PROTECT, related_name='disposals')
    quantity = models.DecimalField(max_digits=15, decimal_places=4)
    unit = models.ForeignKey(UnitOfMeasure, on_delete=models.PROTECT, related_name='+')
    reason = models.TextField()
    reference = models.CharField(max_length=200, blank=True)
    disposal_date = models.DateField()
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='+',
    )
    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'inventory_disposal_record'
        ordering = ['-created_at']
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='disposal_qty_positive'),
        ]

    def save(self, *args, **kwargs):
        if not self.disposal_number:
            prefix = 'SRT' if self.disposal_type == DisposalType.SUPPLIER_RETURN else 'DSP'
            self.disposal_number = generate_reference(prefix, 6)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.disposal_number
