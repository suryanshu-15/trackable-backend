"""
Phase 6 — material requests, FIFO/FEFO allocation & reservations, issue to factory,
returns, disposals / supplier returns and stock adjustments.

Rules enforced here:
  * only eligible, released stock can be reserved or issued (Package.blocking_reasons);
  * FIFO (received date) or FEFO (expiry / retest date) order is enforced; deviating
    requires an authorised role and a reason, or is refused outright by policy;
  * reserved quantity is unavailable to anyone else;
  * every quantity change goes through the ledger, under row locks taken in a fixed order.
"""
import uuid
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum, DecimalField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.config import get_config
from apps.core.exceptions import BusinessRuleViolation, BusinessConflict
from apps.core.models import AuditEvent
from apps.notifications.services import notify_roles

from .models import (
    Package, PackageStatus, QCStatus, MovementReason, IN_WAREHOUSE_STATUSES,
    MaterialRequest, MaterialRequestLine, RequestStatus, OPEN_REQUEST_STATUSES,
    StockReservation, MaterialIssue, MaterialIssueLine, IssueStatus,
    MaterialReturn, DisposalRecord, DisposalType, StockAdjustment,
)
from .services import apply_quantity_change, _create_package_row, _record_movement

ZERO = Decimal('0')
ISSUER_ROLES = ('ADMIN', 'WAREHOUSE', 'MANAGER')
REQUESTER_ROLES = ('ADMIN', 'FACTORY', 'WAREHOUSE')
FAR_FUTURE = date.max


def _require(user, roles, action):
    if not user.has_role(*roles):
        raise BusinessRuleViolation(f'Your role cannot {action}.', code='forbidden', status_code=403)


def _dec(value):
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Reservations & ordering
# ---------------------------------------------------------------------------

def reserved_quantities(package_ids, *, exclude_line=None):
    """Active reserved quantity per package (optionally ignoring one request line's own reservations)."""
    qs = StockReservation.objects.filter(package_id__in=package_ids, is_active=True)
    if exclude_line is not None:
        qs = qs.exclude(request_line=exclude_line)
    return {r['package_id']: r['q'] for r in qs.values('package_id').annotate(q=Sum('reserved_quantity'))}


def reserved_for_line(line):
    return {r.package_id: r for r in line.reservations.filter(is_active=True)}


def uses_fefo(material, packages):
    policy = get_config('issue.allocation_policy')
    if policy in ('FIFO', 'FEFO'):
        return policy == 'FEFO'
    cat = material.category
    if material.requires_expiry or material.requires_retest or (cat and (cat.requires_expiry or cat.requires_retest)):
        return True
    return any(p.expiry_date or p.retest_date for p in packages)


def order_key(pkg, fefo):
    """Sort key; packages with equal keys are interchangeable (same lot / arrival)."""
    if fefo:
        deadline = min(d for d in (pkg.expiry_date, pkg.retest_date, FAR_FUTURE) if d)
        return (deadline, pkg.received_at)
    return (pkg.received_at,)


def partial_issue_allowed(material):
    """Whole packages only when the business rule says so, or the material requires it."""
    return material.allow_partial_issue and not get_config('issue.whole_packages_only')


def candidates(material, unit, *, request_line=None, lock=False):
    """
    Eligible packages for (material, unit) in policy order, with the quantity usable
    by `request_line` (free stock + its own reservations). Returns (rows, fefo).
    """
    qs = Package.objects.available().filter(material=material, unit=unit).select_related('unit')
    if lock:
        qs = qs.select_for_update(of=('self',))
    packages = list(qs.order_by('pk'))
    fefo = uses_fefo(material, packages)
    others = reserved_quantities([p.pk for p in packages], exclude_line=request_line)
    rows = []
    for p in packages:
        usable = p.current_quantity - others.get(p.pk, ZERO)
        if usable > 0:
            rows.append((p, usable))
    rows.sort(key=lambda r: (order_key(r[0], fefo), r[0].package_id))
    return rows, fefo


def suggest(material, unit, quantity, *, request_line=None):
    """Greedy FIFO/FEFO pick list for a quantity. Never includes blocked or reserved-elsewhere stock."""
    rows, fefo = candidates(material, unit, request_line=request_line)
    picks, remaining = [], Decimal(quantity)
    partial = partial_issue_allowed(material)
    for pkg, usable in rows:
        if remaining <= 0:
            break
        if not partial and usable < pkg.current_quantity:
            continue
        take = min(usable, remaining) if partial else (usable if usable <= remaining else ZERO)
        if take <= 0:
            continue
        picks.append({'package': str(pkg.pk), 'package_id': pkg.package_id, 'quantity': str(take),
                      'unit': pkg.unit.symbol, 'batch_number': pkg.batch_number,
                      'received_at': pkg.received_at, 'expiry_date': pkg.expiry_date, 'retest_date': pkg.retest_date})
        remaining -= take
    return {'policy': 'FEFO' if fefo else 'FIFO', 'suggestions': picks, 'shortfall': str(max(remaining, ZERO))}


def fifo_violations(material, unit, selection, *, request_line=None, rows=None):
    """
    A selection is compliant when every package with an earlier order key than a selected
    package is taken in full. Packages reserved for the request line count as compliant
    (their order was checked when they were allocated).
    """
    if rows is None:
        rows, fefo = candidates(material, unit, request_line=request_line)
    else:
        fefo = uses_fefo(material, [r[0] for r in rows])
    own = set(reserved_for_line(request_line)) if request_line is not None else set()
    keyed = [(order_key(p, fefo), p, usable) for p, usable in rows]
    violations = []
    for pkg_id, qty in selection.items():
        if pkg_id in own:
            continue
        sel = next((k for k, p, _ in keyed if p.pk == pkg_id), None)
        if sel is None:
            continue
        skipped = [p.package_id for k, p, usable in keyed
                   if k < sel and selection.get(p.pk, ZERO) < usable]
        if skipped:
            violations.append({'package_id': next(p.package_id for _, p, _ in keyed if p.pk == pkg_id),
                               'older_stock_not_used': skipped})
    return violations, 'FEFO' if fefo else 'FIFO'


# ---------------------------------------------------------------------------
# Material requests
# ---------------------------------------------------------------------------

def _request_snapshot(req):
    return {'status': req.status, 'lines': [
        {'material': l.material.code, 'requested': str(l.requested_quantity), 'issued': str(l.issued_quantity),
         'unit': l.unit.code} for l in req.lines.select_related('material', 'unit')]}


@transaction.atomic
def create_request(data, user, *, request=None):
    _require(user, REQUESTER_ROLES, 'raise material requests')
    lines = data.get('lines') or []
    if not lines:
        raise BusinessRuleViolation('Add at least one material line.', details={'lines': ['Required.']})
    keys = [(l['material'].pk, l['unit'].pk) for l in lines]
    if len(keys) != len(set(keys)):
        raise BusinessRuleViolation('Each material / unit may appear only once per request.')
    for l in lines:
        if not l['material'].is_active:
            raise BusinessRuleViolation(f"{l['material'].code} is inactive.")
    req = MaterialRequest.objects.create(
        factory=data['factory'], department=data.get('department'), requested_by=user,
        required_by=data.get('required_by'), production_order=data.get('production_order', ''),
        cost_centre=data.get('cost_centre', ''), notes=data.get('notes', ''), created_by=user, updated_by=user,
    )
    for l in lines:
        MaterialRequestLine.objects.create(request=req, material=l['material'], requested_quantity=l['quantity'],
                                           unit=l['unit'], notes=l.get('notes', ''))
    record_audit(user, AuditEvent.ACTION_CREATE, 'MaterialRequest', req.id, after=_request_snapshot(req), request=request)
    notify_roles(('WAREHOUSE',), title=f'Material request {req.request_number}',
                 message=f'{req.factory.name}: {len(lines)} line(s)', entity_type='MaterialRequest',
                 entity_id=req.id, link_url=f'/requests/{req.id}', exclude=user)
    return req


def _lock_request(req):
    return MaterialRequest.objects.select_for_update(of=('self',)).select_related('factory').get(pk=req.pk)


def _release_line_reservations(line, user, reason):
    released = []
    for r in line.reservations.filter(is_active=True).select_for_update():
        r.is_active, r.released_at = False, timezone.now()
        r.save(update_fields=['is_active', 'released_at', 'updated_at'])
        released.append(r.package.package_id)
    return released


@transaction.atomic
def allocate_request(req, user, *, request=None):
    """Reserve eligible stock for every open request line in FIFO / FEFO order (may be partial)."""
    _require(user, ('ADMIN', 'WAREHOUSE'), 'allocate stock')
    req = _lock_request(req)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise BusinessRuleViolation(f'Request is {req.get_status_display()}; it cannot be allocated.')
    summary = []
    for line in req.lines.select_related('material', 'unit').select_for_update(of=('self',)):
        # Lock candidate rows so two allocations cannot reserve the same quantity
        candidates(line.material, line.unit, request_line=line, lock=True)
        pick = suggest(line.material, line.unit, line.remaining_quantity, request_line=line)
        target = {uuid.UUID(p['package']): Decimal(p['quantity']) for p in pick['suggestions']}
        current = reserved_for_line(line)
        for pkg_id, res in current.items():
            if pkg_id not in target:
                res.is_active, res.released_at = False, timezone.now()
                res.save(update_fields=['is_active', 'released_at', 'updated_at'])
        for pkg_id, qty in target.items():
            res = current.get(pkg_id)
            if res is None:
                StockReservation.objects.create(package_id=pkg_id, request_line=line, reserved_quantity=qty,
                                                unit=line.unit, reserved_by=user)
            elif res.reserved_quantity != qty:
                res.reserved_quantity = qty
                res.save(update_fields=['reserved_quantity', 'updated_at'])
        summary.append({'material': line.material.code, 'policy': pick['policy'],
                        'reserved': str(sum(target.values(), ZERO)), 'shortfall': pick['shortfall'],
                        'packages': [p['package_id'] for p in pick['suggestions']]})
    if req.status == RequestStatus.SUBMITTED and StockReservation.objects.filter(
            request_line__request=req, is_active=True).exists():
        req.status = RequestStatus.ALLOCATED
    req.updated_by = user
    req.save()
    record_audit(user, AuditEvent.ACTION_UPDATE, 'MaterialRequest', req.id, reason='Stock allocated',
                 after={'allocation': summary}, request=request)
    return req, summary


@transaction.atomic
def release_allocation(req, user, *, reason, request=None):
    _require(user, ('ADMIN', 'WAREHOUSE'), 'release allocations')
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    req = _lock_request(req)
    released = []
    for line in req.lines.all():
        released += _release_line_reservations(line, user, reason)
    if req.status == RequestStatus.ALLOCATED:
        req.status = RequestStatus.SUBMITTED
        req.save(update_fields=['status', 'updated_at'])
    record_audit(user, AuditEvent.ACTION_UPDATE, 'MaterialRequest', req.id, reason=reason.strip(),
                 after={'released_reservations': released}, request=request)
    return req


@transaction.atomic
def cancel_request(req, user, *, reason, request=None):
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    req = _lock_request(req)
    if not (user.has_role('ADMIN', 'WAREHOUSE') or req.requested_by_id == user.pk):
        raise BusinessRuleViolation('Only the requester or the warehouse can cancel this request.', code='forbidden',
                                    status_code=403)
    if req.issues.exists():
        raise BusinessRuleViolation('Material has already been issued; close the request instead.')
    if req.status not in OPEN_REQUEST_STATUSES:
        raise BusinessRuleViolation(f'Request is {req.get_status_display()}.')
    for line in req.lines.all():
        _release_line_reservations(line, user, reason)
    req.status, req.closed_reason, req.updated_by = RequestStatus.CANCELLED, reason.strip(), user
    req.save()
    record_audit(user, AuditEvent.ACTION_TRANSITION, 'MaterialRequest', req.id, reason=reason.strip(),
                 after={'status': req.status}, request=request)
    return req


@transaction.atomic
def close_request(req, user, *, reason, request=None):
    """Short-close a partly issued request; remaining reservations are released."""
    _require(user, ('ADMIN', 'WAREHOUSE'), 'close requests')
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    req = _lock_request(req)
    if req.status not in OPEN_REQUEST_STATUSES:
        raise BusinessRuleViolation(f'Request is {req.get_status_display()}.')
    for line in req.lines.all():
        _release_line_reservations(line, user, reason)
    req.status, req.closed_reason, req.updated_by = RequestStatus.CLOSED, reason.strip(), user
    req.save()
    record_audit(user, AuditEvent.ACTION_TRANSITION, 'MaterialRequest', req.id, reason=reason.strip(),
                 after=_request_snapshot(req), request=request)
    return req


# ---------------------------------------------------------------------------
# Issue
# ---------------------------------------------------------------------------

@transaction.atomic
def create_issue(data, user, *, override_reason='', request=None):
    """
    Issue packages to the factory in one atomic transaction.
    data: factory, department, material_request, production_order, cost_centre, reference,
          received_by, received_by_name, notes, lines=[{package, quantity}]
    """
    _require(user, ISSUER_ROLES, 'issue material')
    lines = data.get('lines') or []
    if not lines:
        raise BusinessRuleViolation('Select at least one package.', details={'lines': ['Required.']})
    ids = [l['package'].pk for l in lines]
    if len(ids) != len(set(ids)):
        raise BusinessRuleViolation('Each package may appear only once per issue.')

    req = data.get('material_request')
    if req is None and get_config('issue.require_request'):
        raise BusinessRuleViolation('Material is issued only against a material request.', code='request_required',
                                    details={'material_request': ['Required.']})
    if req is not None:
        req = _lock_request(req)
        if req.status not in OPEN_REQUEST_STATUSES:
            raise BusinessRuleViolation(f'Request {req.request_number} is {req.get_status_display()}.')
        if data.get('factory') and data['factory'].pk != req.factory_id:
            raise BusinessRuleViolation('The issue factory must match the request.')
    factory = data.get('factory') or (req.factory if req else None)
    if factory is None:
        raise BusinessRuleViolation('A factory is required.', details={'factory': ['Required.']})

    # Lock in a fixed order (by primary key) to avoid deadlocks between concurrent issues
    locked = {p.pk: p for p in Package.objects.select_for_update(of=('self',))
              .select_related('material', 'unit', 'current_location').filter(pk__in=ids).order_by('pk')}
    req_lines = {(l.material_id, l.unit_id): l for l in req.lines.select_for_update(of=('self',))} if req else {}

    problems, plan = [], []
    selection = defaultdict(dict)           # (material, unit, request_line) -> {pkg_id: qty}
    for entry in lines:
        pkg, qty = locked[entry['package'].pk], Decimal(entry['quantity'])
        reasons = pkg.blocking_reasons()
        if reasons:
            problems.append(f'{pkg.package_id}: {", ".join(reasons)}')
            continue
        if qty <= 0:
            problems.append(f'{pkg.package_id}: quantity must be greater than zero')
            continue
        rline = None
        if req is not None:
            rline = req_lines.get((pkg.material_id, pkg.unit_id))
            if rline is None:
                problems.append(f'{pkg.package_id}: {pkg.material.code} ({pkg.unit.code}) is not on request {req.request_number}')
                continue
        reserved_elsewhere = reserved_quantities([pkg.pk], exclude_line=rline).get(pkg.pk, ZERO)
        usable = pkg.current_quantity - reserved_elsewhere
        if qty > usable:
            problems.append(f'{pkg.package_id}: only {usable} {pkg.unit.symbol} is free to issue'
                            + (f' ({reserved_elsewhere} reserved for other requests)' if reserved_elsewhere else ''))
            continue
        if not partial_issue_allowed(pkg.material) and qty != pkg.current_quantity:
            problems.append(f'{pkg.package_id}: only whole packages may be issued ({pkg.current_quantity} {pkg.unit.symbol})')
            continue
        selection[(pkg.material_id, pkg.unit_id, rline.pk if rline else None)][pkg.pk] = qty
        plan.append((pkg, qty, rline))
    if problems:
        raise BusinessRuleViolation('Some packages cannot be issued.', code='issue_rejected',
                                    details={'packages': problems})

    if req is not None:
        per_line = defaultdict(lambda: ZERO)
        for _, qty, rline in plan:
            per_line[rline.pk] += qty
        for rline in req_lines.values():
            if per_line[rline.pk] > rline.remaining_quantity:
                raise BusinessRuleViolation(
                    f'{rline.material.code}: issuing {per_line[rline.pk]} exceeds the {rline.remaining_quantity} '
                    f'{rline.unit.symbol} still requested.', code='exceeds_request')

    # FIFO / FEFO
    violations, policies, noncompliant = [], {}, set()
    for (material_id, unit_id, rline_id), sel in selection.items():
        sample_pkg = locked[next(iter(sel))]
        rline = next((l for l in req_lines.values() if l.pk == rline_id), None)
        found, policy = fifo_violations(sample_pkg.material, sample_pkg.unit, sel, request_line=rline)
        policies[sample_pkg.material.code] = policy
        violations += found
        noncompliant |= {v['package_id'] for v in found}
    if violations:
        if get_config('issue.fifo_override_policy') == 'FORBID':
            raise BusinessRuleViolation('Selection does not follow FIFO/FEFO order and overrides are not permitted.',
                                        code='fifo_violation', details={'violations': violations})
        if not (override_reason or '').strip():
            raise BusinessConflict('Selection skips older / earlier-expiring stock. An authorised override with a '
                                   'reason is required.', code='fifo_override_required',
                                   details={'violations': violations, 'policy': policies})
        if not user.has_role(*get_config('issue.override_roles')):
            raise BusinessRuleViolation('Your role cannot override FIFO/FEFO order.', code='forbidden', status_code=403)

    issue = MaterialIssue.objects.create(
        material_request=req, factory=factory, department=data.get('department') or (req.department if req else None),
        production_order=data.get('production_order') or (req.production_order if req else ''),
        cost_centre=data.get('cost_centre') or (req.cost_centre if req else ''),
        reference=data.get('reference', ''), issued_by=user, received_by=data.get('received_by'),
        received_by_name=data.get('received_by_name', ''), issue_datetime=timezone.now(), notes=data.get('notes', ''),
        fifo_override=bool(violations), fifo_override_reason=(override_reason or '').strip() if violations else '',
        fifo_override_approved_by=user if violations else None, created_by=user, updated_by=user,
    )
    for pkg, qty, rline in plan:
        apply_quantity_change(pkg, -qty, reason=MovementReason.ISSUE, user=user, reference_id=issue.issue_number)
        if pkg.current_quantity == 0:
            pkg.mark_issued()
            pkg.save()
        MaterialIssueLine.objects.create(
            issue=issue, package=pkg, material=pkg.material, request_line=rline, issued_quantity=qty, unit=pkg.unit,
            qc_status_at_issue=pkg.qc_status, batch_number=pkg.batch_number,
            fifo_compliant=pkg.package_id not in noncompliant,
        )
        if rline is not None:
            res = rline.reservations.filter(package=pkg, is_active=True).first()
            if res:
                res.reserved_quantity = max(res.reserved_quantity - qty, ZERO)
                if res.reserved_quantity == 0:
                    res.is_active, res.released_at = False, timezone.now()
                res.save()
            rline.issued_quantity += qty
            rline.save(update_fields=['issued_quantity', 'updated_at'])
        record_audit(user, AuditEvent.ACTION_ISSUE, 'Package', pkg.id, after={'issued': str(qty),
                     'balance': str(pkg.current_quantity)}, extra={'issue': issue.issue_number}, request=request)

    if req is not None:
        if all(l.remaining_quantity == 0 for l in req.lines.all()):
            req.status = RequestStatus.FULFILLED
            for line in req.lines.all():
                _release_line_reservations(line, user, 'Request fulfilled')
        else:
            req.status = RequestStatus.PARTIALLY_ISSUED
        req.updated_by = user
        req.save()

    record_audit(user, AuditEvent.ACTION_ISSUE, 'MaterialIssue', issue.id,
                 after={'issue_number': issue.issue_number, 'lines': len(plan), 'policy': policies,
                        'request': req.request_number if req else None}, request=request)
    if violations:
        record_audit(user, AuditEvent.ACTION_OVERRIDE, 'MaterialIssue', issue.id, reason=issue.fifo_override_reason,
                     after={'violations': violations}, request=request)
        notify_roles(('MANAGER', 'ADMIN'), title=f'FIFO/FEFO override on {issue.issue_number}',
                     message=f'{user.display_name}: {issue.fifo_override_reason}', level='WARNING',
                     entity_type='MaterialIssue', entity_id=issue.id, link_url=f'/issues/{issue.id}', exclude=user)
    return issue


@transaction.atomic
def acknowledge_issue(issue, user, *, request=None):
    issue = MaterialIssue.objects.select_for_update().get(pk=issue.pk)
    allowed = user.has_role('ADMIN') or (user.has_role('FACTORY') and (
        issue.received_by_id in (None, user.pk)
        and (issue.material_request is None or issue.material_request.requested_by_id == user.pk
             or issue.received_by_id == user.pk)))
    if not allowed:
        raise BusinessRuleViolation('Only the receiving factory user can acknowledge this issue.', code='forbidden',
                                    status_code=403)
    if issue.status != IssueStatus.ISSUED:
        raise BusinessRuleViolation('This issue has already been acknowledged.')
    issue.status, issue.acknowledged_by, issue.acknowledged_at = IssueStatus.ACKNOWLEDGED, user, timezone.now()
    issue.save(update_fields=['status', 'acknowledged_by', 'acknowledged_at', 'updated_at'])
    record_audit(user, AuditEvent.ACTION_APPROVE, 'MaterialIssue', issue.id, after={'status': issue.status},
                 request=request)
    return issue


# ---------------------------------------------------------------------------
# Returns, disposals, adjustments (Stage 9)
# ---------------------------------------------------------------------------

@transaction.atomic
def create_return(data, user, *, request=None):
    """
    Returned material becomes a new child package (new ID and label) that is blocked until
    cleared: RETEST_REQUIRED when QC is required, otherwise an inspection hold.
    """
    from apps.laboratory.models import QualityHold, HoldType
    from apps.core.utils import generate_reference

    _require(user, ('ADMIN', 'WAREHOUSE'), 'receive returns')
    reason = (data.get('reason') or '').strip()
    if not reason:
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    line = MaterialIssueLine.objects.select_for_update(of=('self',)).select_related(
        'package', 'issue', 'unit', 'material').get(pk=data['issue_line'].pk)
    qty = Decimal(data['returned_quantity'])
    returned = line.returns.aggregate(q=Coalesce(Sum('returned_quantity'), ZERO, output_field=DecimalField()))['q']
    if qty <= 0 or returned + qty > line.issued_quantity:
        raise BusinessRuleViolation(
            f'At most {line.issued_quantity - returned} {line.unit.symbol} can still be returned on this line.',
            code='exceeds_issued')
    parent = line.package
    requires_qc = data.get('requires_qc', True) and line.material.qc_required
    ret = MaterialReturn.objects.create(
        issue_line=line, returned_quantity=qty, unit=line.unit, reason=reason,
        returned_by_name=data.get('returned_by_name', ''), received_by=user, requires_qc=requires_qc,
        notes=data.get('notes', ''), created_by=user, updated_by=user,
    )
    child = _create_package_row(
        invoice_line_id=parent.invoice_line_id, parent_package=parent, split_reason=f'Return {ret.return_number}',
        material_id=parent.material_id, supplier_id=parent.supplier_id, original_quantity=qty, current_quantity=qty,
        unit_id=parent.unit_id, batch_number=parent.batch_number, manufacturing_date=parent.manufacturing_date,
        expiry_date=parent.expiry_date, retest_date=parent.retest_date, grade=parent.grade,
        received_at=parent.received_at, is_recalled=parent.is_recalled,
        qc_status=QCStatus.RETEST_REQUIRED if requires_qc else line.qc_status_at_issue,
        created_by=user, updated_by=user,
    )
    _record_movement(child, reason=MovementReason.RETURN, user=user, quantity=qty, delta=qty,
                     reference_id=ret.return_number, notes=reason)
    ret.return_package = child
    ret.save(update_fields=['return_package', 'updated_at'])
    if not requires_qc:
        hold = QualityHold.objects.create(hold_type=HoldType.HOLD, reference=generate_reference('HLD', 6),
                                          reason=f'Return inspection {ret.return_number}: {reason}', held_by=user,
                                          held_at=timezone.now(), created_by=user, updated_by=user)
        hold.packages.add(child)
        Package.objects.filter(pk=child.pk).update(is_on_hold=True)
    record_audit(user, AuditEvent.ACTION_RETURN, 'Package', child.id, reason=reason,
                 after={'quantity': str(qty), 'qc_status': child.qc_status, 'inspection_hold': not requires_qc},
                 extra={'return': ret.return_number, 'issue': line.issue.issue_number, 'parent_package': parent.package_id},
                 request=request)
    record_audit(user, AuditEvent.ACTION_RETURN, 'MaterialIssue', line.issue_id, reason=reason,
                 after={'return': ret.return_number, 'package': child.package_id, 'quantity': str(qty)}, request=request)
    notify_roles(('LAB_REVIEWER',) if not requires_qc else ('LAB_TECH', 'LAB_REVIEWER'),
                 title=f'Returned material {child.package_id} needs clearance',
                 message=f'{ret.return_number}: {qty} {line.unit.symbol} {line.material.code}',
                 entity_type='Package', entity_id=child.id, link_url=f'/packages/{child.id}', exclude=user)
    return ret


@transaction.atomic
def create_disposal(data, user, *, request=None):
    """Scrap or return to supplier. Disposing of good, released stock needs ADMIN / MANAGER."""
    _require(user, ('ADMIN', 'WAREHOUSE', 'MANAGER'), 'dispose of stock')
    reason = (data.get('reason') or '').strip()
    if not reason:
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    pkg = Package.objects.select_for_update(of=('self',)).select_related('unit', 'current_location').get(pk=data['package'].pk)
    if pkg.status not in IN_WAREHOUSE_STATUSES:
        raise BusinessRuleViolation(f'{pkg.package_id} is {pkg.get_status_display()}.')
    qty = Decimal(data['quantity'])
    reserved = reserved_quantities([pkg.pk]).get(pkg.pk, ZERO)
    if qty <= 0 or qty > pkg.current_quantity - reserved:
        raise BusinessRuleViolation(f'At most {pkg.current_quantity - reserved} {pkg.unit.symbol} can be disposed of'
                                    + (' (the rest is reserved)' if reserved else '') + '.', code='insufficient_quantity')
    if pkg.is_available and not user.has_role('ADMIN', 'MANAGER'):
        raise BusinessRuleViolation('Disposing of released, usable stock requires a manager.', code='forbidden',
                                    status_code=403)
    kind = data.get('disposal_type') or DisposalType.SCRAP
    rec = DisposalRecord.objects.create(
        disposal_type=kind, package=pkg, quantity=qty, unit=pkg.unit, reason=reason,
        reference=data.get('reference', ''), disposal_date=data.get('disposal_date') or timezone.localdate(),
        authorized_by=user, notes=data.get('notes', ''), created_by=user, updated_by=user,
    )
    before = pkg.status
    apply_quantity_change(pkg, -qty, user=user, reference_id=rec.disposal_number, notes=reason,
                          reason=MovementReason.SUPPLIER_RETURN if kind == DisposalType.SUPPLIER_RETURN else MovementReason.DISPOSAL)
    if pkg.current_quantity == 0:
        pkg.return_to_supplier() if kind == DisposalType.SUPPLIER_RETURN else pkg.dispose()
        pkg.save()
    record_audit(user, AuditEvent.ACTION_ADJUST, 'Package', pkg.id, reason=reason,
                 before={'status': before}, after={'disposed': str(qty), 'type': kind, 'status': pkg.status},
                 extra={'disposal': rec.disposal_number}, request=request)
    return rec


@transaction.atomic
def request_adjustment(data, user, *, request=None):
    _require(user, ('ADMIN', 'WAREHOUSE'), 'request stock adjustments')
    pkg = data['package']
    if pkg.status not in IN_WAREHOUSE_STATUSES:
        raise BusinessRuleViolation(f'{pkg.package_id} is {pkg.get_status_display()}; adjust only stock in the warehouse.')
    if data['unit'].pk != pkg.unit_id:
        raise BusinessRuleViolation('Adjustment unit must match the package unit.')
    if not (data.get('reason') or '').strip():
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    adj = StockAdjustment.objects.create(
        package=pkg, adjustment_type=data['adjustment_type'], quantity_change=data['quantity_change'], unit=data['unit'],
        reason=data['reason'].strip(), reference=data.get('reference', ''), created_by=user, updated_by=user,
    )
    record_audit(user, AuditEvent.ACTION_CREATE, 'StockAdjustment', adj.id, reason=adj.reason,
                 after={'type': adj.adjustment_type, 'quantity': str(adj.quantity_change), 'package': pkg.package_id},
                 request=request)
    notify_roles(('MANAGER',), title=f'Stock adjustment awaiting approval: {pkg.package_id}',
                 message=f'{adj.get_adjustment_type_display()} {adj.quantity_change} {pkg.unit.symbol}: {adj.reason}',
                 entity_type='StockAdjustment', entity_id=adj.id, link_url='/stock-control?tab=adjustments', exclude=user)
    return adj


@transaction.atomic
def decide_adjustment(adj, user, *, approve, notes='', request=None):
    _require(user, ('ADMIN', 'MANAGER'), 'approve stock adjustments')
    adj = StockAdjustment.objects.select_for_update(of=('self',)).get(pk=adj.pk)
    if adj.status != 'PENDING':
        raise BusinessRuleViolation('Only pending adjustments can be decided.')
    if adj.created_by_id == user.pk:
        raise BusinessRuleViolation('You cannot approve or reject your own adjustment.', code='separation_of_duties',
                                    status_code=403)
    if not approve and not (notes or '').strip():
        raise BusinessRuleViolation('Give a reason for rejecting.', details={'notes': ['Required.']})
    if approve:
        pkg = Package.objects.select_for_update(of=('self',)).select_related('unit', 'current_location').get(pk=adj.package_id)
        delta = {'INCREASE': adj.quantity_change, 'DECREASE': -adj.quantity_change}.get(
            adj.adjustment_type, adj.quantity_change - pkg.current_quantity)
        reserved = reserved_quantities([pkg.pk]).get(pkg.pk, ZERO)
        if pkg.current_quantity + delta < reserved:
            raise BusinessRuleViolation(f'{reserved} {pkg.unit.symbol} of {pkg.package_id} is reserved; release it first.')
        apply_quantity_change(pkg, delta, reason=MovementReason.ADJUSTMENT, user=user, reference_id=str(adj.id),
                              notes=adj.reason)
        adj.applied_delta = delta
        record_audit(user, AuditEvent.ACTION_ADJUST, 'Package', pkg.id, reason=adj.reason,
                     after={'delta': str(delta), 'balance': str(pkg.current_quantity)},
                     extra={'adjustment': str(adj.id)}, request=request)
    adj.status = 'APPROVED' if approve else 'REJECTED'
    adj.approved_by, adj.approved_at, adj.approval_notes, adj.updated_by = user, timezone.now(), notes, user
    adj.save()
    record_audit(user, AuditEvent.ACTION_APPROVE if approve else AuditEvent.ACTION_REJECT, 'StockAdjustment', adj.id,
                 reason=notes, after={'status': adj.status, 'applied_delta': _dec(adj.applied_delta)}, request=request)
    return adj


def request_line_reconciliation(req):
    """Cached issued quantities must equal the issue lines (spec: quantities must reconcile)."""
    out = []
    for line in req.lines.all():
        actual = line.issue_lines.aggregate(q=Coalesce(Sum('issued_quantity'), Value(ZERO), output_field=DecimalField()))['q']
        if actual != line.issued_quantity:
            out.append({'material': line.material.code, 'cached': str(line.issued_quantity), 'actual': str(actual)})
    return out
