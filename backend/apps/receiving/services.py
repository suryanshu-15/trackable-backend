"""
Receiving service layer — gate receipt, invoice lines, weight correction, documents.
Business rules live here; views only translate HTTP to these calls.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Max, Sum
from django_fsm import can_proceed, TransitionNotAllowed

from apps.core.audit import record_audit
from apps.core.config import get_config, get_decimal
from apps.core.exceptions import BusinessRuleViolation, BusinessConflict
from apps.core.models import AuditEvent
from apps.core.uploads import validate_upload

from django.utils import timezone

from apps.master.models import UoMConversion

from .models import (
    GoodsReceipt, InvoiceLine, ReceiptDocument, ReceiptStatus,
    ReceiptLineVerification, VerificationStatus, DiscrepancyStatus,
)

# Receipt header fields may be edited freely only while the receipt is a draft.
EDITABLE_RECEIPT_STATUSES = {ReceiptStatus.DRAFT}
# Invoice lines are entered at the gate (draft) or straight after registration.
LINE_EDITABLE_STATUSES = {ReceiptStatus.DRAFT, ReceiptStatus.GATE_REGISTERED}
# Documents can be attached until the receipt is closed or cancelled.
DOCUMENT_BLOCKED_STATUSES = {ReceiptStatus.CLOSED, ReceiptStatus.CANCELLED}

WEIGHT_FIELDS = ('gross_weight', 'tare_weight', 'net_weight', 'weight_unit_id', 'weighing_method', 'weighbridge_reference')


def _weights_snapshot(receipt):
    return {
        f: (str(getattr(receipt, f)) if getattr(receipt, f) is not None else None)
        for f in WEIGHT_FIELDS
    }


# ---------------------------------------------------------------------------
# Duplicate invoice check
# ---------------------------------------------------------------------------

def find_duplicate_receipts(*, supplier, invoice_number, invoice_date, exclude_pk=None):
    """
    Return non-cancelled receipts matching the configured duplicate key.
    Invoice number alone is never treated as globally unique.
    """
    values = {'supplier': supplier, 'invoice_number': invoice_number, 'invoice_date': invoice_date}
    fields = get_config('receipt.duplicate_check_fields') or []
    lookup = {}
    for field in fields:
        if field not in values or values[field] in (None, ''):
            return GoodsReceipt.objects.none()
        lookup['invoice_number__iexact' if field == 'invoice_number' else field] = values[field]
    if not lookup:
        return GoodsReceipt.objects.none()
    qs = GoodsReceipt.objects.filter(**lookup).exclude(status=ReceiptStatus.CANCELLED)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    return qs


def _check_duplicates(data, acknowledge_duplicate, exclude_pk=None):
    dupes = find_duplicate_receipts(
        supplier=data.get('supplier'),
        invoice_number=data.get('invoice_number'),
        invoice_date=data.get('invoice_date'),
        exclude_pk=exclude_pk,
    )
    refs = list(dupes.values_list('receipt_number', flat=True)[:10])
    if refs and not acknowledge_duplicate:
        raise BusinessConflict(
            'A receipt already exists for this supplier invoice. '
            'Resubmit with acknowledge_duplicate=true to proceed.',
            code='duplicate_invoice',
            details={'duplicate_receipts': refs},
        )
    return refs


# ---------------------------------------------------------------------------
# Receipt lifecycle
# ---------------------------------------------------------------------------

def _validate_weights(gross, tare):
    if gross is not None and gross < 0:
        raise BusinessRuleViolation('Gross weight cannot be negative.', details={'gross_weight': ['Must be ≥ 0.']})
    if tare is not None and tare < 0:
        raise BusinessRuleViolation('Tare weight cannot be negative.', details={'tare_weight': ['Must be ≥ 0.']})
    if gross is not None and tare is not None and gross < tare:
        raise BusinessRuleViolation(
            'Tare weight cannot exceed gross weight.',
            details={'tare_weight': ['Tare weight cannot exceed gross weight.']},
        )


@transaction.atomic
def create_receipt(data, user, *, acknowledge_duplicate=False, request=None):
    """Create a DRAFT receipt. `data` is validated serializer data."""
    _validate_weights(data.get('gross_weight'), data.get('tare_weight'))
    dupes = _check_duplicates(data, acknowledge_duplicate)
    receipt = GoodsReceipt(**data, created_by=user, updated_by=user)
    receipt.save()
    record_audit(
        user, AuditEvent.ACTION_CREATE, 'GoodsReceipt', receipt.id,
        after={'receipt_number': receipt.receipt_number, 'status': receipt.status, **_weights_snapshot(receipt)},
        extra={'duplicate_acknowledged': dupes} if dupes else None,
        request=request,
    )
    return receipt


@transaction.atomic
def update_draft_receipt(receipt, data, user, *, acknowledge_duplicate=False, request=None):
    """Edit header fields. Allowed only while DRAFT; afterwards use correct_weights()."""
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status not in EDITABLE_RECEIPT_STATUSES:
        raise BusinessRuleViolation(
            f'Receipt {receipt.receipt_number} is {receipt.get_status_display()}; '
            'submitted gate data can only be changed through the correction workflow.',
            code='receipt_locked',
        )
    merged = {
        'supplier': data.get('supplier', receipt.supplier),
        'invoice_number': data.get('invoice_number', receipt.invoice_number),
        'invoice_date': data.get('invoice_date', receipt.invoice_date),
    }
    if any(k in data for k in merged):
        _check_duplicates(merged, acknowledge_duplicate, exclude_pk=receipt.pk)
    _validate_weights(data.get('gross_weight', receipt.gross_weight), data.get('tare_weight', receipt.tare_weight))

    before = {k: str(getattr(receipt, k)) for k in data}
    for field, value in data.items():
        setattr(receipt, field, value)
    receipt.updated_by = user
    receipt.save()
    record_audit(
        user, AuditEvent.ACTION_UPDATE, 'GoodsReceipt', receipt.id,
        before=before, after={k: str(getattr(receipt, k)) for k in data}, request=request,
    )
    return receipt


@transaction.atomic
def correct_weights(receipt, data, user, *, reason, request=None):
    """
    Authorized correction of weighing data after the draft stage.
    Never silently overwrites: before/after values and the reason are audited.
    """
    if not reason or not reason.strip():
        raise BusinessRuleViolation('A reason is required for weight corrections.', details={'reason': ['Required.']})
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    if receipt.status in (ReceiptStatus.CLOSED, ReceiptStatus.CANCELLED):
        raise BusinessRuleViolation(f'Receipt {receipt.receipt_number} is {receipt.get_status_display()} and cannot be corrected.')
    allowed = {'gross_weight', 'tare_weight', 'weight_unit', 'weighing_method', 'weighbridge_reference'}
    changes = {k: v for k, v in data.items() if k in allowed}
    if not changes:
        raise BusinessRuleViolation('No weight fields supplied.')
    _validate_weights(changes.get('gross_weight', receipt.gross_weight), changes.get('tare_weight', receipt.tare_weight))

    before = _weights_snapshot(receipt)
    for field, value in changes.items():
        setattr(receipt, field, value)
    receipt.updated_by = user
    receipt.save()
    record_audit(
        user, AuditEvent.ACTION_UPDATE, 'GoodsReceipt', receipt.id,
        reason=reason.strip(), before=before, after=_weights_snapshot(receipt),
        extra={'kind': 'WEIGHT_CORRECTION'}, request=request,
    )
    return receipt


def _require_lines(receipt):
    if not receipt.invoice_lines.exists():
        raise BusinessRuleViolation(
            'Add at least one invoice line before submitting for unloading.', code='no_invoice_lines',
        )


def unresolved_lines(receipt, *, require_packaging):
    """
    Lines that block completion: not verified, discrepant without an accepted
    exception, or (when packaging is required) accepted quantity not fully packaged.
    """
    from apps.inventory.services import packaged_quantity
    problems = []
    for line in receipt.invoice_lines.select_related('verification', 'verification__received_unit'):
        v = getattr(line, 'verification', None)
        if v is None or v.verification_status == VerificationStatus.PENDING:
            problems.append({'line_number': line.line_number, 'problem': 'not verified'})
            continue
        if v.verification_status == VerificationStatus.DISCREPANT:
            problems.append({'line_number': line.line_number, 'problem': 'discrepancy awaiting review'})
            continue
        if require_packaging:
            packaged = packaged_quantity(line)
            accepted = v.accepted_quantity or Decimal('0')
            if packaged != accepted:
                problems.append({
                    'line_number': line.line_number,
                    'problem': f'packaged {packaged} of {accepted} {v.received_unit.symbol} accepted',
                })
    return problems


def _require_verified(receipt):
    problems = unresolved_lines(receipt, require_packaging=False)
    if problems:
        raise BusinessRuleViolation(
            'All lines must be verified, and any discrepancies accepted, first.',
            code='lines_unresolved', details={'lines': problems},
        )


def _require_unloading_complete(receipt):
    problems = unresolved_lines(receipt, require_packaging=True)
    if problems:
        raise BusinessRuleViolation(
            'Receipt cannot be marked unloaded while quantities remain unresolved or unpackaged.',
            code='lines_unresolved', details={'lines': problems},
        )


def _require_putaway_complete(receipt):
    from apps.inventory.services import receipt_has_pending_putaway
    if receipt_has_pending_putaway(receipt):
        raise BusinessRuleViolation('Some packages are still awaiting put-away.', code='putaway_incomplete')


# Transition name → (roles allowed, extra precondition). Only names listed here
# can be invoked through the API.
RECEIPT_TRANSITIONS = {
    'register_at_gate': (('ADMIN', 'GATE'), None),
    'submit_for_unloading': (('ADMIN', 'GATE'), _require_lines),
    'start_unloading': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), None),
    'mark_verification_pending': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), _require_verified),
    'flag_exception': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), None),
    'mark_unloaded': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), _require_unloading_complete),
    'start_putaway': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), None),
    'mark_stored': (('ADMIN', 'RECEIVING', 'WAREHOUSE'), _require_putaway_complete),
    'close': (('ADMIN', 'MANAGER'), None),
    # The gate can cancel its own work before the truck is sent for unloading (the only states cancel allows).
    'cancel': (('ADMIN', 'GATE'), None),
}
REASON_REQUIRED_TRANSITIONS = {'cancel', 'flag_exception'}


def allowed_receipt_transitions(receipt, user):
    """Transitions valid from the current state that this user may perform."""
    names = [t.name for t in receipt.get_available_status_transitions()]
    return [n for n in names if n in RECEIPT_TRANSITIONS and user.has_role(*RECEIPT_TRANSITIONS[n][0])]


@transaction.atomic
def transition_receipt(receipt, name, user, *, reason='', request=None):
    if name not in RECEIPT_TRANSITIONS:
        raise BusinessRuleViolation(f'Unknown transition: {name}.', code='unknown_transition')
    roles, precondition = RECEIPT_TRANSITIONS[name]
    if not user.has_role(*roles):
        raise BusinessRuleViolation('Your role cannot perform this transition.', code='forbidden', status_code=403)
    if name in REASON_REQUIRED_TRANSITIONS and not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required for this action.', details={'reason': ['Required.']})

    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    method = getattr(receipt, name)
    if not can_proceed(method):
        raise TransitionNotAllowed(
            f'Cannot {name.replace("_", " ")} a receipt that is {receipt.get_status_display()}.'
        )
    if precondition:
        precondition(receipt)
    before = receipt.status
    method()
    receipt.updated_by = user
    receipt.save()
    record_audit(
        user, AuditEvent.ACTION_TRANSITION, 'GoodsReceipt', receipt.id,
        reason=reason, before={'status': before}, after={'status': receipt.status},
        extra={'transition': name}, request=request,
    )
    return receipt


# ---------------------------------------------------------------------------
# Invoice lines
# ---------------------------------------------------------------------------

def _assert_lines_editable(receipt):
    if receipt.status not in LINE_EDITABLE_STATUSES:
        raise BusinessRuleViolation(
            f'Invoice lines cannot be changed once the receipt is {receipt.get_status_display()}.',
            code='receipt_locked',
        )


def _line_snapshot(line):
    return {
        'line_number': line.line_number,
        'material': str(line.material_id) if line.material_id else None,
        'material_name_declared': line.material_name_declared,
        'declared_quantity': str(line.declared_quantity),
        'declared_unit': line.declared_unit_id,
        'declared_weight': str(line.declared_weight) if line.declared_weight is not None else None,
        'batch_number': line.batch_number,
    }


@transaction.atomic
def add_invoice_line(receipt, data, user, *, request=None):
    receipt = GoodsReceipt.objects.select_for_update().get(pk=receipt.pk)
    _assert_lines_editable(receipt)
    required_tests = data.pop('required_tests', None)
    if not data.get('line_number'):
        current = receipt.invoice_lines.aggregate(m=Max('line_number'))['m'] or 0
        data['line_number'] = current + 1
    elif receipt.invoice_lines.filter(line_number=data['line_number']).exists():
        raise BusinessRuleViolation(
            f"Line number {data['line_number']} already exists on this receipt.",
            details={'line_number': ['Already used.']},
        )
    material = data.get('material')
    if material and not data.get('material_name_declared'):
        data['material_name_declared'] = material.name
    line = InvoiceLine.objects.create(receipt=receipt, created_by=user, updated_by=user, **data)
    if required_tests is not None:
        line.required_tests.set(required_tests)
    record_audit(
        user, AuditEvent.ACTION_CREATE, 'InvoiceLine', line.id,
        after=_line_snapshot(line), extra={'receipt': str(receipt.id)}, request=request,
    )
    return line


@transaction.atomic
def update_invoice_line(line, data, user, *, request=None):
    receipt = GoodsReceipt.objects.select_for_update().get(pk=line.receipt_id)
    _assert_lines_editable(receipt)
    data.pop('receipt', None)
    required_tests = data.pop('required_tests', None)
    new_number = data.get('line_number')
    if new_number and new_number != line.line_number and \
            receipt.invoice_lines.filter(line_number=new_number).exclude(pk=line.pk).exists():
        raise BusinessRuleViolation(f'Line number {new_number} already exists on this receipt.')
    before = _line_snapshot(line)
    for field, value in data.items():
        setattr(line, field, value)
    line.updated_by = user
    line.save()
    if required_tests is not None:
        line.required_tests.set(required_tests)
    record_audit(
        user, AuditEvent.ACTION_UPDATE, 'InvoiceLine', line.id,
        before=before, after=_line_snapshot(line), extra={'receipt': str(receipt.id)}, request=request,
    )
    return line


@transaction.atomic
def delete_invoice_line(line, user, *, request=None):
    receipt = GoodsReceipt.objects.select_for_update().get(pk=line.receipt_id)
    _assert_lines_editable(receipt)
    if line.packages.exists() or hasattr(line, 'verification'):
        raise BusinessRuleViolation('This line already has packages or verification records and cannot be removed.')
    record_audit(
        user, AuditEvent.ACTION_DELETE, 'InvoiceLine', line.id,
        before=_line_snapshot(line), extra={'receipt': str(receipt.id)}, request=request,
    )
    line.required_tests.clear()
    line.delete()


# ---------------------------------------------------------------------------
# Unloading verification (Phase 3)
# ---------------------------------------------------------------------------

VERIFIABLE_RECEIPT_STATUSES = {ReceiptStatus.UNLOADING_IN_PROGRESS, ReceiptStatus.EXCEPTION}
ZERO = Decimal('0')


def convert_quantity(quantity, from_unit, to_unit):
    """Explicit, configured conversion only. Returns None when no rule exists."""
    if from_unit.pk == to_unit.pk:
        return quantity
    rule = UoMConversion.objects.filter(from_unit=from_unit, to_unit=to_unit).first()
    if rule:
        return quantity * rule.factor
    reverse_rule = UoMConversion.objects.filter(from_unit=to_unit, to_unit=from_unit).first()
    if reverse_rule and reverse_rule.factor:
        return quantity / reverse_rule.factor
    return None


def _verification_snapshot(v):
    fields = ('received_quantity', 'accepted_quantity', 'short_quantity', 'excess_quantity',
              'damaged_quantity', 'mismatched_quantity')
    snap = {f: (str(getattr(v, f)) if getattr(v, f) is not None else None) for f in fields}
    snap.update({
        'received_unit': v.received_unit_id,
        'identified_material': v.identified_material_id,
        'verification_status': v.verification_status,
        'discrepancy_status': v.discrepancy_status,
    })
    return snap


def _classify(line, v):
    """Server-side discrepancy classification. Declared invoice values are only read, never changed."""
    kinds = []
    expected = convert_quantity(line.declared_quantity, line.declared_unit, v.received_unit)
    v.short_quantity = v.excess_quantity = None
    if expected is None:
        kinds.append(DiscrepancyStatus.UNIT_MISMATCH)
    else:
        diff = v.received_quantity - expected
        v.short_quantity = max(-diff, ZERO)
        v.excess_quantity = max(diff, ZERO)
        if v.short_quantity > 0:
            kinds.append(DiscrepancyStatus.QUANTITY_SHORT)
        if v.excess_quantity > 0:
            kinds.append(DiscrepancyStatus.QUANTITY_EXCESS)
    if v.received_quantity > 0 and not v.accepted_quantity:
        kinds.append(DiscrepancyStatus.REJECTED)
    if v.damaged_quantity:
        kinds.append(DiscrepancyStatus.DAMAGED)
    if v.mismatched_quantity:
        kinds.append(DiscrepancyStatus.UNIDENTIFIED)
    material = line.material or v.identified_material
    if v.accepted_quantity and material is None:
        kinds.append(DiscrepancyStatus.UNIDENTIFIED)
    grade_differs = bool(v.observed_grade) and v.observed_grade.strip().lower() != (line.grade_or_specification or '').strip().lower()
    material_differs = bool(line.material_id and v.identified_material_id and line.material_id != v.identified_material_id)
    if v.observed_description.strip() or grade_differs or material_differs:
        kinds.append(DiscrepancyStatus.DESCRIPTION_MISMATCH)
    kinds = list(dict.fromkeys(kinds))
    if not kinds:
        return DiscrepancyStatus.NONE, VerificationStatus.VERIFIED, []
    status = kinds[0] if len(kinds) == 1 else DiscrepancyStatus.MULTIPLE
    return status, VerificationStatus.DISCREPANT, [str(k) for k in kinds]


@transaction.atomic
def record_verification(line, data, user, *, request=None):
    """
    Record (or re-record) what was physically received for an invoice line.
    Short/excess and the discrepancy status are computed here, not taken from the client.
    """
    from apps.inventory.services import packaged_quantity

    receipt = GoodsReceipt.objects.select_for_update().get(pk=line.receipt_id)
    if receipt.status not in VERIFIABLE_RECEIPT_STATUSES:
        raise BusinessRuleViolation(
            f'Lines can be verified only while unloading; receipt is {receipt.get_status_display()}.',
            code='receipt_not_unloading',
        )
    line = InvoiceLine.objects.select_related('material', 'declared_unit').get(pk=line.pk)
    v = ReceiptLineVerification.objects.select_for_update().filter(invoice_line=line).first()
    before = _verification_snapshot(v) if v else None
    if v is None:
        v = ReceiptLineVerification(invoice_line=line, created_by=user)

    received = data['received_quantity']
    damaged = data.get('damaged_quantity') or ZERO
    mismatched = data.get('mismatched_quantity') or ZERO
    if min(received, damaged, mismatched) < 0:
        raise BusinessRuleViolation('Quantities cannot be negative.')
    if damaged + mismatched > received:
        raise BusinessRuleViolation(
            'Damaged + mismatched quantity cannot exceed the received quantity.',
            details={'damaged_quantity': ['Exceeds received.']},
        )
    good = received - damaged - mismatched
    accepted = data.get('accepted_quantity')
    accepted = good if accepted is None else accepted
    if accepted < 0 or accepted > good:
        raise BusinessRuleViolation(
            f'Accepted quantity must be between 0 and {good} (received − damaged − mismatched).',
            details={'accepted_quantity': ['Out of range.']},
        )
    if get_config('receiving.line_acceptance') == 'WHOLE' and accepted not in (ZERO, received):
        raise BusinessRuleViolation(
            'An invoice line is accepted in full or rejected: accept everything received '
            '(no damaged or mismatched quantity), or reject the line (accepted quantity 0).',
            code='partial_acceptance_not_allowed', details={'accepted_quantity': ['Accept all or nothing.']},
        )
    unit_codes = get_config('package.unit_codes') or []
    if accepted > 0 and unit_codes and data['received_unit'].code not in unit_codes:
        raise BusinessRuleViolation(
            f'Packages are counted in {", ".join(unit_codes)}; record the received quantity in that unit.',
            code='package_unit_not_allowed', details={'received_unit': ['Not a package unit.']},
        )
    if v.pk:
        if v.received_unit_id and data['received_unit'].pk != v.received_unit_id and line.packages.exists():
            raise BusinessRuleViolation('The unit cannot change after packages have been created.')
        already = packaged_quantity(line)
        if accepted < already:
            raise BusinessRuleViolation(
                f'{already} is already packaged for this line; accepted quantity cannot go below that.',
                code='below_packaged_quantity',
            )

    v.received_quantity = received
    v.received_unit = data['received_unit']
    v.damaged_quantity = damaged
    v.mismatched_quantity = mismatched
    v.accepted_quantity = accepted
    v.identified_material = data.get('identified_material')
    for field in ('observed_description', 'observed_grade', 'verification_notes', 'discrepancy_notes'):
        setattr(v, field, data.get(field, '') or '')
    v.unloading_start = data.get('unloading_start') or v.unloading_start or receipt.unloading_started_at
    v.unloading_end = data.get('unloading_end') or timezone.now()
    if v.unloading_end and v.unloading_start and v.unloading_end < v.unloading_start:
        raise BusinessRuleViolation('Unloading end cannot be before unloading start.')
    v.discrepancy_status, v.verification_status, kinds = _classify(line, v)
    # Any previous exception acceptance is void once the figures are re-recorded
    v.exception_authorized_by = None
    v.exception_reason = ''
    v.verified_by = user
    v.updated_by = user
    v.save()

    record_audit(
        user, AuditEvent.ACTION_CREATE if before is None else AuditEvent.ACTION_UPDATE,
        'ReceiptLineVerification', v.id, before=before, after=_verification_snapshot(v),
        extra={'receipt': str(receipt.id), 'invoice_line': str(line.id), 'discrepancies': kinds},
        request=request,
    )

    if v.verification_status == VerificationStatus.DISCREPANT and receipt.status == ReceiptStatus.UNLOADING_IN_PROGRESS:
        receipt.flag_exception()
        receipt.updated_by = user
        receipt.save()
        record_audit(
            user, AuditEvent.ACTION_TRANSITION, 'GoodsReceipt', receipt.id,
            reason=f'Automatic: discrepancy on line {line.line_number} ({", ".join(kinds)})',
            before={'status': ReceiptStatus.UNLOADING_IN_PROGRESS}, after={'status': receipt.status},
            extra={'transition': 'flag_exception', 'system': True}, request=request,
        )
    return v


DISCREPANCY_REVIEWER_ROLES = ('ADMIN', 'MANAGER')


@transaction.atomic
def accept_discrepancy(verification, user, *, reason, request=None):
    """Authorized acceptance of a discrepant line as recorded (the exception path)."""
    if not user.has_role(*DISCREPANCY_REVIEWER_ROLES):
        raise BusinessRuleViolation('Your role cannot accept discrepancies.', code='forbidden', status_code=403)
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required to accept a discrepancy.', details={'reason': ['Required.']})
    v = ReceiptLineVerification.objects.select_for_update(of=('self',)).select_related('invoice_line__receipt').get(pk=verification.pk)
    if v.verification_status != VerificationStatus.DISCREPANT:
        raise BusinessRuleViolation('Only discrepant lines awaiting review can be accepted.')
    if v.invoice_line.receipt.status not in VERIFIABLE_RECEIPT_STATUSES | {ReceiptStatus.VERIFICATION_PENDING}:
        raise BusinessRuleViolation('The receipt is no longer in unloading.')
    if get_config('receiving.discrepancy_acceptance_separation') and v.verified_by_id == user.pk:
        raise BusinessRuleViolation(
            'The person who recorded the discrepancy cannot also accept it.', code='separation_of_duties', status_code=403,
        )
    before = _verification_snapshot(v)
    v.verification_status = VerificationStatus.EXCEPTION_ACCEPTED
    v.exception_authorized_by = user
    v.exception_reason = reason.strip()
    v.updated_by = user
    v.save()
    record_audit(
        user, AuditEvent.ACTION_APPROVE, 'ReceiptLineVerification', v.id, reason=reason.strip(),
        before=before, after=_verification_snapshot(v),
        extra={'receipt': str(v.invoice_line.receipt_id), 'invoice_line': str(v.invoice_line_id)}, request=request,
    )
    return v


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

@transaction.atomic
def attach_document(receipt, uploaded_file, document_type, user, *, notes='', request=None):
    if receipt.status in DOCUMENT_BLOCKED_STATUSES:
        raise BusinessRuleViolation(f'Documents cannot be added to a {receipt.get_status_display().lower()} receipt.')
    detected_mime = validate_upload(uploaded_file)
    doc = ReceiptDocument.objects.create(
        receipt=receipt,
        document_type=document_type,
        file=uploaded_file,
        original_filename=uploaded_file.name[:255],
        file_size_bytes=uploaded_file.size,
        mime_type=detected_mime,
        notes=notes,
        uploaded_by=user,
    )
    record_audit(
        user, AuditEvent.ACTION_UPLOAD, 'ReceiptDocument', doc.id,
        after={'document_type': document_type, 'filename': doc.original_filename, 'size': doc.file_size_bytes},
        extra={'receipt': str(receipt.id)}, request=request,
    )
    return doc


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------

def weight_discrepancy(receipt):
    """
    Compare gate net weight with the sum of declared line weights (same unit only).
    Returns None when there is nothing comparable; never infers product quantity.
    """
    if receipt.net_weight is None or receipt.weight_unit_id is None:
        return None
    declared = receipt.invoice_lines.filter(
        declared_weight__isnull=False, declared_weight_unit_id=receipt.weight_unit_id,
    ).aggregate(total=Sum('declared_weight'))['total']
    if declared is None:
        return None
    diff = receipt.net_weight - declared
    pct = (abs(diff) / declared * 100) if declared else Decimal('100')
    tolerance = get_decimal('receipt.weight_tolerance_percent')
    return {
        'net_weight': str(receipt.net_weight),
        'declared_line_weight': str(declared),
        'difference': str(diff),
        'difference_percent': str(round(pct, 2)),
        'tolerance_percent': str(tolerance),
        'exceeds_tolerance': pct > tolerance,
    }


def receipt_timeline(receipt):
    """Chronological audit trail for the receipt, its lines and documents."""
    line_ids = [str(i) for i in receipt.invoice_lines.values_list('id', flat=True)]
    doc_ids = [str(i) for i in receipt.documents.values_list('id', flat=True)]
    rid = str(receipt.id)
    from django.db.models import Q
    return AuditEvent.objects.filter(
        Q(entity_type='GoodsReceipt', entity_id=rid)
        | Q(entity_type='InvoiceLine', entity_id__in=line_ids)
        | Q(entity_type='InvoiceLine', extra__receipt=rid)  # includes deleted lines
        | Q(entity_type='ReceiptDocument', entity_id__in=doc_ids)
        | Q(entity_type='ReceiptLineVerification', extra__receipt=rid)
    ).select_related('actor').order_by('timestamp')
