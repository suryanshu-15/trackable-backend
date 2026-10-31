"""
Inventory service layer — packages, labels, scanning, put-away, transfers,
repacking, the movement ledger, stock queries and reconciliation.
Business rules live here; views only translate HTTP to these calls.
"""
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Sum, Count, Q, F, Subquery, OuterRef, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.config import get_config
from apps.core.exceptions import BusinessRuleViolation
from apps.core.models import AuditEvent
from apps.master.models import Material, StorageLocation
from apps.receiving.models import (
    InvoiceLine, GoodsReceipt, ReceiptStatus, VerificationStatus,
)

from .models import (
    Package, PackageStatus, QCStatus, InventoryMovement, MovementReason,
    MaterialIssueLine, PackageLabelEvent, IN_WAREHOUSE_STATUSES,
)

ZERO = Decimal('0')
MAX_PACKAGES_PER_REQUEST = 500

# Receipt states in which packages may be generated for verified lines
PACKAGING_RECEIPT_STATUSES = {
    ReceiptStatus.UNLOADING_IN_PROGRESS, ReceiptStatus.EXCEPTION, ReceiptStatus.VERIFICATION_PENDING,
}
LABEL_PRINTABLE_STATUSES = set(IN_WAREHOUSE_STATUSES)


def _snapshot(pkg):
    return {
        'status': pkg.status,
        'qc_status': pkg.qc_status,
        'current_quantity': str(pkg.current_quantity),
        'location': str(pkg.current_location) if pkg.current_location_id else None,
    }


def _lock(pkg):
    return Package.objects.select_for_update(of=('self',)).select_related('current_location', 'unit').get(pk=pkg.pk)


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

def _record_movement(pkg, *, reason, user, quantity, delta=ZERO, from_location=None, to_location=None,
                     location_changed=False, reference_id='', notes=''):
    return InventoryMovement.objects.create(
        package=pkg, reason=reason, performed_by=user,
        quantity=quantity, quantity_delta=delta, balance_after=pkg.current_quantity,
        from_location=from_location, to_location=to_location, location_changed=location_changed,
        unit=pkg.unit, reference_id=reference_id, notes=notes,
    )


def apply_quantity_change(pkg, delta, *, reason, user, reference_id='', notes=''):
    """
    The only way to change a package balance. `pkg` must already be locked
    (select_for_update) by the caller's transaction. Writes the ledger entry.
    """
    delta = Decimal(delta)
    new_balance = pkg.current_quantity + delta
    if new_balance < 0:
        raise BusinessRuleViolation(
            f'{pkg.package_id} holds {pkg.current_quantity} {pkg.unit.symbol}; cannot remove {abs(delta)}.',
            code='insufficient_quantity',
        )
    pkg.current_quantity = new_balance
    pkg.save(update_fields=['current_quantity', 'updated_at', 'updated_by'])
    return _record_movement(
        pkg, reason=reason, user=user, quantity=abs(delta), delta=delta,
        from_location=pkg.current_location if delta < 0 else None,
        to_location=pkg.current_location if delta > 0 else None,
        reference_id=reference_id, notes=notes,
    )


# ---------------------------------------------------------------------------
# Package creation (Phase 3)
# ---------------------------------------------------------------------------

def line_material(line):
    verification = getattr(line, 'verification', None)
    return line.material or (verification.identified_material if verification else None)


def packaged_quantity(line):
    """Quantity already packaged for an invoice line (top-level packages only; repacks don't add stock)."""
    return line.packages.filter(parent_package__isnull=True).aggregate(
        total=Coalesce(Sum('original_quantity'), ZERO, output_field=DecimalField()))['total']


def _create_package_row(**fields):
    """Insert a package, regenerating its random ID on the (rare) unique collision."""
    for _ in range(5):
        try:
            with transaction.atomic():
                return Package.objects.create(**fields)
        except IntegrityError as exc:
            if 'package_id' not in str(exc) and 'scan_token' not in str(exc):
                raise
    raise BusinessRuleViolation('Could not allocate a unique package ID; please retry.')


@transaction.atomic
def create_packages(line, specs, user, *, request=None):
    """
    Create one traceable package per spec for a verified invoice line.
    specs: list of dicts with `quantity` and optional lot overrides
    (batch_number, manufacturing_date, expiry_date, retest_date, grade, notes).
    """
    if not specs:
        raise BusinessRuleViolation('No packages requested.')
    if len(specs) > MAX_PACKAGES_PER_REQUEST:
        raise BusinessRuleViolation(f'At most {MAX_PACKAGES_PER_REQUEST} packages per request.')

    line = InvoiceLine.objects.select_for_update(of=('self',)).select_related('receipt', 'material').get(pk=line.pk)
    receipt = line.receipt
    if receipt.status not in PACKAGING_RECEIPT_STATUSES:
        raise BusinessRuleViolation(
            f'Packages can only be created while unloading; receipt {receipt.receipt_number} is '
            f'{receipt.get_status_display()}.', code='receipt_not_unloading',
        )
    verification = getattr(line, 'verification', None)
    if verification is None or verification.verification_status not in (
            VerificationStatus.VERIFIED, VerificationStatus.EXCEPTION_ACCEPTED):
        raise BusinessRuleViolation(
            f'Line {line.line_number} must be verified (or its discrepancy accepted) before packages are created.',
            code='line_not_verified',
        )
    material = line_material(line)
    if material is None:
        raise BusinessRuleViolation(
            f'Line {line.line_number} has no master material. Record the identified material in the verification first.',
            code='material_unidentified',
        )

    quantities = [Decimal(str(s['quantity'])) for s in specs]
    if any(q <= 0 for q in quantities):
        raise BusinessRuleViolation('Every package quantity must be greater than zero.')
    accepted = verification.accepted_quantity or ZERO
    already = packaged_quantity(line)
    requested = sum(quantities, ZERO)
    if already + requested > accepted:
        raise BusinessRuleViolation(
            f'Line {line.line_number}: accepted {accepted} {verification.received_unit.symbol}, '
            f'already packaged {already}, requested {requested}. Packages cannot exceed the accepted quantity.',
            code='exceeds_accepted_quantity',
            details={'accepted': str(accepted), 'packaged': str(already), 'remaining': str(accepted - already)},
        )

    unit_codes = get_config('package.unit_codes') or []
    if unit_codes and verification.received_unit.code not in unit_codes:
        raise BusinessRuleViolation(
            f'Packages are counted in {", ".join(unit_codes)}; re-verify line {line.line_number} in that unit.',
            code='package_unit_not_allowed')

    needs_expiry = material.requires_expiry or bool(material.category and material.category.requires_expiry)
    needs_retest = material.requires_retest or bool(material.category and material.category.requires_retest)
    qc_status = QCStatus.PENDING_SAMPLING if material.qc_required else QCStatus.NOT_REQUIRED

    created = []
    for spec, qty in zip(specs, quantities):
        lot = {
            'batch_number': spec.get('batch_number') or line.batch_number,
            'manufacturing_date': spec.get('manufacturing_date') or line.manufacturing_date,
            'expiry_date': spec.get('expiry_date') or line.expiry_date,
            'retest_date': spec.get('retest_date') or line.retest_date,
            'grade': spec.get('grade') or verification.observed_grade or line.grade_or_specification,
        }
        if needs_expiry and not lot['expiry_date']:
            raise BusinessRuleViolation(f'{material.code} requires an expiry date.', details={'expiry_date': ['Required.']})
        if needs_retest and not lot['retest_date']:
            raise BusinessRuleViolation(f'{material.code} requires a retest date.', details={'retest_date': ['Required.']})
        pkg = _create_package_row(
            invoice_line=line, material=material, supplier=receipt.supplier,
            original_quantity=qty, current_quantity=qty, unit=verification.received_unit,
            received_at=receipt.arrival_datetime, qc_status=qc_status,
            notes=spec.get('notes', ''), created_by=user, updated_by=user, **lot,
        )
        _record_movement(pkg, reason=MovementReason.RECEIPT, user=user, quantity=qty, delta=qty,
                         reference_id=receipt.receipt_number)
        record_audit(user, AuditEvent.ACTION_CREATE, 'Package', pkg.id, after=_snapshot(pkg),
                     extra={'receipt': str(receipt.id), 'invoice_line': str(line.id)}, request=request)
        created.append(pkg)

    record_audit(
        user, AuditEvent.ACTION_CREATE, 'InvoiceLine', line.id,
        reason=f'{len(created)} package(s) created',
        after={'packages': [p.package_id for p in created], 'packaged_quantity': str(already + requested)},
        extra={'receipt': str(receipt.id), 'kind': 'PACKAGES_CREATED'}, request=request,
    )
    return created


# ---------------------------------------------------------------------------
# Labels & scanning
# ---------------------------------------------------------------------------

def qr_payload(pkg):
    """Opaque, stable content for the QR code. Never includes mutable status."""
    return f'{settings.FRONTEND_BASE_URL}/scan/{pkg.scan_token}'


def label_data(pkg):
    line = pkg.invoice_line
    return {
        'package_id': pkg.package_id,
        'package_type': get_config('package.type') or '',
        'qr_payload': qr_payload(pkg),
        'barcode_value': pkg.package_id,
        'material_code': pkg.material.code,
        'material_name': pkg.material.name,
        'quantity': str(pkg.original_quantity),
        'unit': pkg.unit.symbol,
        'batch_number': pkg.batch_number,
        'grade': pkg.grade,
        'manufacturing_date': pkg.manufacturing_date,
        'expiry_date': pkg.expiry_date,
        'retest_date': pkg.retest_date,
        'supplier': pkg.supplier.name,
        'receipt_number': line.receipt.receipt_number,
        'line_number': line.line_number,
        'received_date': timezone.localtime(pkg.received_at).date(),
        'parent_package_id': pkg.parent_package.package_id if pkg.parent_package_id else None,
    }


@transaction.atomic
def record_label_print(pkg, user, *, printer_reference='', reason='', request=None):
    """
    First print moves CREATED → AWAITING_PUTAWAY. Reprints keep the same package ID
    and require a reason.
    """
    pkg = _lock(pkg)
    if pkg.status not in LABEL_PRINTABLE_STATUSES:
        raise BusinessRuleViolation(f'Labels cannot be printed for a package that is {pkg.get_status_display()}.')
    event_type = 'PRINT' if pkg.label_print_count == 0 else 'REPRINT'
    if event_type == 'REPRINT' and not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required for label reprints.', details={'reason': ['Required.']})
    before = _snapshot(pkg)
    if pkg.status == PackageStatus.CREATED:
        pkg.mark_awaiting_putaway()
    pkg.label_print_count += 1
    pkg.label_printed_at = timezone.now()
    pkg.updated_by = user
    pkg.save()
    event = PackageLabelEvent.objects.create(
        package=pkg, event_type=event_type, printed_by=user,
        printer_reference=printer_reference, reprint_reason=reason.strip(),
    )
    record_audit(user, AuditEvent.ACTION_PRINT, 'Package', pkg.id, reason=reason.strip(),
                 before=before, after=_snapshot(pkg),
                 extra={'event_type': event_type, 'print_count': pkg.label_print_count}, request=request)
    return pkg, event


@transaction.atomic
def record_release_tag(pkg, user, *, reason='', printer_reference='', request=None):
    """
    Green release tag. A visual indicator only — printed (or reprinted) solely when the
    server confirms the package is quality-released and not blocked.
    """
    pkg = _lock(pkg)
    if pkg.status not in (PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED):
        raise BusinessRuleViolation(f'{pkg.package_id} is {pkg.get_status_display()}; no release tag can be printed.')
    blockers = [r for r in pkg.blocking_reasons() if not r.startswith('status is')]
    if blockers:
        raise BusinessRuleViolation(
            f'{pkg.package_id} is not released: {", ".join(blockers)}.', code='not_released',
            details={'blocking_reasons': blockers})
    reprint = pkg.label_events.filter(event_type='RELEASE_TAG').exists()
    if reprint and not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required to reprint a release tag.', details={'reason': ['Required.']})
    event = PackageLabelEvent.objects.create(
        package=pkg, event_type='RELEASE_TAG', printed_by=user,
        printer_reference=printer_reference, reprint_reason=(reason or '').strip(),
    )
    record_audit(user, AuditEvent.ACTION_PRINT, 'Package', pkg.id, reason=(reason or '').strip(),
                 after={'qc_status': pkg.qc_status}, extra={'event_type': 'RELEASE_TAG', 'reprint': reprint},
                 request=request)
    return pkg, event


def resolve_scan(code, user, *, request=None):
    """
    Resolve scanner input to a package: a QR URL (…/scan/<token>), a bare token,
    or a printed package ID (keyboard-wedge barcode). Records the scan.
    """
    code = (code or '').strip()
    if not code:
        raise BusinessRuleViolation('Scan code is empty.')
    token = code.rstrip('/').rsplit('/scan/', 1)[-1] if '/scan/' in code else code
    pkg = (Package.objects.filter(scan_token=token).first()
           or Package.objects.filter(package_id__iexact=code).first())
    if pkg is None:
        raise BusinessRuleViolation('No package matches this code.', code='not_found', status_code=404)
    record_audit(user, AuditEvent.ACTION_SCAN, 'Package', pkg.id, request=request)
    return pkg


# ---------------------------------------------------------------------------
# Locations, put-away, transfer (Phase 4)
# ---------------------------------------------------------------------------

def validate_storage_location(location):
    if not location.is_active or not location.warehouse.is_active:
        raise BusinessRuleViolation(f'Location {location} is inactive.', code='location_inactive')
    if location.children.filter(is_active=True).exists():
        raise BusinessRuleViolation(
            f'{location} contains sub-locations; choose a specific shelf or bin.', code='location_not_storable',
        )


def _assert_quarantine_rule(pkg, location):
    """Material awaiting or failing QC must sit in a quarantine location."""
    if pkg.requires_quarantine and not location.is_quarantine:
        raise BusinessRuleViolation(
            f'{pkg.package_id} is {pkg.get_qc_status_display()} and must be stored in a quarantine / pending-QC location.',
            code='quarantine_required',
        )


def _advance_receipt_after_putaway(receipt_id, user, request):
    """Receipt moves UNLOADED → PUTAWAY_IN_PROGRESS → STORED as its packages are put away."""
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt_id)
    changed = []
    if receipt.status == ReceiptStatus.UNLOADED:
        receipt.start_putaway()
        changed.append('start_putaway')
    if receipt.status == ReceiptStatus.PUTAWAY_IN_PROGRESS and not receipt_has_pending_putaway(receipt):
        receipt.mark_stored()
        changed.append('mark_stored')
    if changed:
        before = receipt.status
        receipt.updated_by = user
        receipt.save()
        record_audit(user, AuditEvent.ACTION_TRANSITION, 'GoodsReceipt', receipt.id,
                     reason='Automatic: put-away progress', after={'status': receipt.status},
                     extra={'transition': changed, 'system': True}, request=request)


def receipt_has_pending_putaway(receipt):
    return Package.objects.filter(
        invoice_line__receipt=receipt,
        status__in=[PackageStatus.CREATED, PackageStatus.AWAITING_PUTAWAY],
    ).exists()


@transaction.atomic
def putaway_package(pkg, location, user, *, notes='', request=None):
    pkg = _lock(pkg)
    if pkg.status == PackageStatus.CREATED:
        raise BusinessRuleViolation(f'Print the label for {pkg.package_id} before put-away.', code='label_not_printed')
    if pkg.status != PackageStatus.AWAITING_PUTAWAY:
        raise BusinessRuleViolation(f'{pkg.package_id} is {pkg.get_status_display()} and is not awaiting put-away.')
    receipt_status = pkg.invoice_line.receipt.status
    if pkg.parent_package_id is None and receipt_status not in (
            ReceiptStatus.UNLOADED, ReceiptStatus.PUTAWAY_IN_PROGRESS, ReceiptStatus.STORED, ReceiptStatus.CLOSED):
        raise BusinessRuleViolation('Complete unloading of the receipt before put-away.', code='receipt_not_unloaded')
    location = StorageLocation.objects.select_related('warehouse').get(pk=location.pk)
    validate_storage_location(location)
    _assert_quarantine_rule(pkg, location)

    before = _snapshot(pkg)
    from_location = pkg.current_location
    pkg.store()
    pkg.current_location = location
    pkg.updated_by = user
    pkg.save()
    _record_movement(pkg, reason=MovementReason.PUTAWAY, user=user, quantity=pkg.current_quantity,
                     from_location=from_location, to_location=location, location_changed=True, notes=notes)
    record_audit(user, AuditEvent.ACTION_TRANSITION, 'Package', pkg.id, reason=f'Put-away to {location}',
                 before=before, after=_snapshot(pkg), request=request)
    _advance_receipt_after_putaway(pkg.invoice_line.receipt_id, user, request)
    return pkg


@transaction.atomic
def transfer_package(pkg, to_location, user, *, notes='', request=None):
    pkg = _lock(pkg)
    if pkg.status != PackageStatus.STORED:
        raise BusinessRuleViolation(f'Only stored packages can be transferred; {pkg.package_id} is {pkg.get_status_display()}.')
    to_location = StorageLocation.objects.select_related('warehouse').get(pk=to_location.pk)
    if pkg.current_location_id == to_location.pk:
        raise BusinessRuleViolation('Source and destination locations are the same.')
    validate_storage_location(to_location)
    _assert_quarantine_rule(pkg, to_location)

    from_location = pkg.current_location
    before = _snapshot(pkg)
    pkg.current_location = to_location
    pkg.updated_by = user
    pkg.save(update_fields=['current_location', 'updated_by', 'updated_at'])
    qc_move = bool(from_location and from_location.is_quarantine) != to_location.is_quarantine
    _record_movement(pkg, reason=MovementReason.QC_MOVE if qc_move else MovementReason.TRANSFER, user=user,
                     quantity=pkg.current_quantity, from_location=from_location, to_location=to_location,
                     location_changed=True, notes=notes)
    record_audit(user, AuditEvent.ACTION_UPDATE, 'Package', pkg.id, reason=f'Transfer to {to_location}',
                 before=before, after=_snapshot(pkg), request=request)
    return pkg


@transaction.atomic
def split_package(pkg, quantities, user, *, reason, request=None):
    """
    Repack: move quantities from a parent into new child packages (new IDs, new labels).
    The parent keeps its history; parent/child linkage and quantities are recorded in the ledger.
    """
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required to split a package.', details={'reason': ['Required.']})
    pkg = _lock(pkg)
    if pkg.status not in (PackageStatus.AWAITING_PUTAWAY, PackageStatus.STORED):
        raise BusinessRuleViolation(f'{pkg.package_id} is {pkg.get_status_display()} and cannot be split.')
    quantities = [Decimal(str(q)) for q in quantities]
    if not quantities or any(q <= 0 for q in quantities):
        raise BusinessRuleViolation('Each child quantity must be greater than zero.')
    total = sum(quantities, ZERO)
    from .issuing import reserved_quantities
    reserved = reserved_quantities([pkg.pk]).get(pkg.pk, ZERO)
    if reserved and total > pkg.current_quantity - reserved:
        raise BusinessRuleViolation(f'{reserved} {pkg.unit.symbol} of {pkg.package_id} is reserved for a request.',
                                    code='insufficient_quantity')
    if total > pkg.current_quantity:
        raise BusinessRuleViolation(
            f'Split total {total} exceeds the {pkg.current_quantity} {pkg.unit.symbol} in {pkg.package_id}.',
            code='insufficient_quantity',
        )
    before = _snapshot(pkg)
    children = []
    for qty in quantities:
        child = _create_package_row(
            invoice_line_id=pkg.invoice_line_id, parent_package=pkg, split_reason=reason.strip(),
            material_id=pkg.material_id, supplier_id=pkg.supplier_id,
            original_quantity=qty, current_quantity=qty, unit_id=pkg.unit_id,
            batch_number=pkg.batch_number, manufacturing_date=pkg.manufacturing_date,
            expiry_date=pkg.expiry_date, retest_date=pkg.retest_date, grade=pkg.grade,
            received_at=pkg.received_at, qc_status=pkg.qc_status,
            is_on_hold=pkg.is_on_hold, is_recalled=pkg.is_recalled,
            current_location=pkg.current_location, created_by=user, updated_by=user,
        )
        apply_quantity_change(pkg, -qty, reason=MovementReason.SPLIT_OUT, user=user,
                              reference_id=child.package_id, notes=reason.strip())
        _record_movement(child, reason=MovementReason.SPLIT_IN, user=user, quantity=qty, delta=qty,
                         to_location=child.current_location, location_changed=child.current_location_id is not None,
                         reference_id=pkg.package_id, notes=reason.strip())
        record_audit(user, AuditEvent.ACTION_CREATE, 'Package', child.id, reason=reason.strip(),
                     after=_snapshot(child), extra={'parent_package': pkg.package_id}, request=request)
        children.append(child)
    if pkg.current_quantity == 0:
        pkg.mark_repacked()
        pkg.save()
    record_audit(user, AuditEvent.ACTION_UPDATE, 'Package', pkg.id, reason=reason.strip(),
                 before=before, after=_snapshot(pkg),
                 extra={'kind': 'SPLIT', 'children': [c.package_id for c in children]}, request=request)
    return pkg, children


# ---------------------------------------------------------------------------
# Stock queries (Phase 4)
# ---------------------------------------------------------------------------

GROUP_FIELDS = {
    'material': ['material_id', 'material__code', 'material__name'],
    'lot': ['batch_number'],
    'location': ['current_location_id', 'current_location__code', 'current_location__warehouse__code'],
    'qc_status': ['qc_status'],
    'status': ['status'],
    'supplier': ['supplier_id', 'supplier__name'],
}


def stock_balances(qs, group_by):
    """Aggregate in-warehouse stock. Unit is always part of the key — quantities in different units are never summed."""
    fields = []
    for g in group_by:
        fields += GROUP_FIELDS[g]
    fields += ['unit__code', 'unit__symbol']
    from .models import StockReservation
    available_qty = Subquery(Package.objects.available().filter(pk=OuterRef('pk')).values('current_quantity')[:1])
    reserved_qty = Subquery(StockReservation.objects.filter(package=OuterRef('pk'), is_active=True)
                            .values('package').annotate(q=Sum('reserved_quantity')).values('q')[:1])
    rows = (
        qs.in_warehouse()
        .annotate(avail=Coalesce(available_qty, Value(ZERO), output_field=DecimalField()),
                  reserved=Coalesce(reserved_qty, Value(ZERO), output_field=DecimalField()))
        .values(*fields)
        .annotate(packages=Count('id'), quantity=Sum('current_quantity'), released_quantity=Sum('avail'),
                  reserved_quantity=Sum('reserved'))
        .order_by(*fields)
    )
    # available = released and not reserved. Decimals as strings: JSON floats would lose precision.
    return [{**r, 'quantity': str(r['quantity']), 'released_quantity': str(r['released_quantity']),
             'reserved_quantity': str(r['reserved_quantity']),
             'available_quantity': str(max(r['released_quantity'] - r['reserved_quantity'], ZERO))} for r in rows]


def ageing_buckets():
    bounds = sorted(int(b) for b in get_config('stock.ageing_buckets'))
    labels, lower = [], 0
    for upper in bounds:
        labels.append((f'{lower}-{upper}', lower, upper))
        lower = upper + 1
    labels.append((f'{lower}+', lower, None))
    return labels


def stock_ageing(qs):
    """Quantity per material/unit per age bucket, by received (FIFO) date."""
    today = timezone.localdate()
    buckets = ageing_buckets()
    result = defaultdict(lambda: {b[0]: ZERO for b in buckets})
    meta = {}
    for row in qs.in_warehouse().values('material_id', 'material__code', 'material__name',
                                         'unit__symbol', 'received_at', 'current_quantity'):
        age = (today - timezone.localtime(row['received_at']).date()).days
        label = next(b[0] for b in buckets if b[2] is None or age <= b[2])
        key = (row['material_id'], row['unit__symbol'])
        result[key][label] += row['current_quantity']
        meta[key] = (row['material__code'], row['material__name'])
    return {
        'buckets': [b[0] for b in buckets],
        'rows': [
            {'material_id': k[0], 'material_code': meta[k][0], 'material_name': meta[k][1], 'unit': k[1],
             'quantities': {label: str(v) for label, v in vals.items()}}
            for k, vals in sorted(result.items(), key=lambda kv: meta[kv[0]][0])
        ],
    }


def expiry_exposure(qs, days=None):
    """In-warehouse packages expired / due for retest, or becoming so within `days`."""
    days = int(days if days is not None else get_config('stock.near_expiry_days'))
    horizon = timezone.localdate() + timedelta(days=days)
    return qs.in_warehouse().filter(
        Q(expiry_date__lte=horizon) | Q(retest_date__lte=horizon)
    ).order_by(Coalesce('expiry_date', 'retest_date'))


def low_stock_materials():
    """Materials whose available quantity (in their threshold unit) is below the configured minimum."""
    out = []
    for m in Material.objects.filter(is_active=True, min_stock_quantity__isnull=False).select_related('min_stock_unit', 'default_unit'):
        unit = m.min_stock_unit or m.default_unit
        if unit is None:
            continue
        available = Package.objects.available().filter(material=m, unit=unit).aggregate(
            q=Coalesce(Sum('current_quantity'), ZERO, output_field=DecimalField()))['q']
        if available < m.min_stock_quantity:
            out.append({'material_id': m.pk, 'material_code': m.code, 'material_name': m.name,
                        'available': str(available), 'minimum': str(m.min_stock_quantity), 'unit': unit.symbol})
    return out


def reconciliation_issues(qs=None):
    """
    Packages whose cached balance or location disagrees with the movement ledger.
    An empty list means stock reconciles.
    """
    qs = qs if qs is not None else Package.objects.all()
    last_location = InventoryMovement.objects.filter(
        package=OuterRef('pk'), location_changed=True,
    ).order_by('-created_at').values('to_location')[:1]
    rows = qs.annotate(
        ledger_qty=Coalesce(Sum('movements__quantity_delta'), ZERO, output_field=DecimalField()),
        ledger_location=Subquery(last_location),
    ).values('id', 'package_id', 'current_quantity', 'ledger_qty', 'current_location_id', 'ledger_location')
    issues = []
    for r in rows:
        problems = []
        if r['ledger_qty'] != r['current_quantity']:
            problems.append(f"balance {r['current_quantity']} ≠ ledger {r['ledger_qty']}")
        if r['ledger_location'] != r['current_location_id']:
            problems.append('location differs from last ledger movement')
        if problems:
            issues.append({'id': str(r['id']), 'package_id': r['package_id'], 'problems': problems})
    return issues


def package_timeline(pkg):
    """
    Chronological lifecycle: gate receipt → unloading → packaging → scans, labels,
    movements, laboratory events, issues, returns, adjustments.
    """
    events = []
    receipt = pkg.invoice_line.receipt
    lineage = [pkg]
    parent = pkg.parent_package
    while parent is not None:  # include history inherited from repacked parents
        lineage.append(parent)
        parent = parent.parent_package

    def add(ts, kind, title, actor=None, **details):
        events.append({'timestamp': ts, 'kind': kind, 'title': title,
                       'actor': actor.display_name if actor else None, 'details': details})

    for e in AuditEvent.objects.filter(entity_type='GoodsReceipt', entity_id=str(receipt.id)).select_related('actor'):
        add(e.timestamp, 'RECEIPT', f'Receipt {receipt.receipt_number}: {e.action.lower()}'
            + (f" → {e.after_state.get('status')}" if e.after_state and e.after_state.get('status') else ''),
            e.actor, reason=e.reason)
    for e in AuditEvent.objects.filter(entity_type='ReceiptLineVerification', extra__invoice_line=str(pkg.invoice_line_id)).select_related('actor'):
        add(e.timestamp, 'VERIFICATION', f'Line {pkg.invoice_line.line_number} verification {e.action.lower()}',
            e.actor, reason=e.reason, after=e.after_state)
    package_ids = [str(p.id) for p in lineage]
    for e in AuditEvent.objects.filter(entity_type='Package', entity_id__in=package_ids).select_related('actor'):
        own = e.entity_id == str(pkg.id)
        label = {'CREATE': 'Package created', 'PRINT': 'Label printed', 'SCAN': 'Scanned',
                 'TRANSITION': 'Status change', 'UPDATE': 'Updated'}.get(e.action, e.action.title())
        add(e.timestamp, e.action, label if own else f'{label} (parent package)', e.actor,
            reason=e.reason, before=e.before_state, after=e.after_state, extra=e.extra)
    for m in InventoryMovement.objects.filter(package=pkg).select_related('from_location', 'to_location', 'performed_by'):
        add(m.created_at, 'MOVEMENT', m.get_reason_display(), m.performed_by,
            quantity_delta=str(m.quantity_delta), balance_after=str(m.balance_after),
            from_location=str(m.from_location) if m.from_location else None,
            to_location=str(m.to_location) if m.to_location else None, reference=m.reference_id)
    sample_ids = set()
    for p in lineage:
        sample_ids |= {str(i) for i in p.sample_packages.values_list('sample_id', flat=True)}
    if sample_ids:
        lab_labels = {'LabSample': 'Sample', 'LabTestResult': 'Test result', 'QualityDisposition': 'Quality disposition'}
        for e in AuditEvent.objects.filter(
                Q(entity_type='LabSample', entity_id__in=sample_ids)
                | Q(entity_type__in=['LabTestResult', 'QualityDisposition'], extra__sample__in=sample_ids)
        ).select_related('actor'):
            add(e.timestamp, 'LAB', f"{lab_labels[e.entity_type]} {e.action.lower()}"
                + (f" ({e.extra.get('sample_id')})" if e.extra and e.extra.get('sample_id') else ''),
                e.actor, reason=e.reason, after=e.after_state)
    for line in MaterialIssueLine.objects.filter(package=pkg).select_related('issue', 'issue__issued_by'):
        add(line.created_at, 'ISSUE', f'Issued on {line.issue.issue_number}', line.issue.issued_by,
            quantity=str(line.issued_quantity), qc_status_at_issue=line.qc_status_at_issue)
    events.sort(key=lambda e: e['timestamp'])
    return events
