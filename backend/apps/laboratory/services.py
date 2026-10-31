"""
Laboratory service layer — sampling, results, review, disposition, holds and recalls.
Business rules live here; views only translate HTTP to these calls.

Quality status changes only here. Physical lifecycle and location are never touched.
"""
from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.audit import record_audit
from apps.core.config import get_config
from apps.core.exceptions import BusinessRuleViolation
from apps.core.models import AuditEvent
from apps.core.uploads import validate_upload
from apps.core.utils import generate_reference
from apps.inventory.models import (
    Package, QCStatus, MovementReason, MaterialIssueLine, IN_WAREHOUSE_STATUSES,
)
from apps.inventory.services import apply_quantity_change
from apps.notifications.services import notify_roles

from .models import (
    LabSample, SamplePackage, LabTestRun, LabTestResult, LabSampleAttachment,
    QualityDisposition, QualityHold, HoldType,
    SampleStatus, TestRunStatus, TestResultStatus, DispositionDecision, ACTIVE_SAMPLE_STATUSES,
)

LAB_ROLES = ('ADMIN', 'LAB_TECH', 'LAB_REVIEWER')
REVIEWER_ROLES = ('ADMIN', 'LAB_REVIEWER')
# QC statuses from which a package may be (re)sampled. PASSED allows periodic retest.
SAMPLEABLE_QC = (QCStatus.PENDING_SAMPLING, QCStatus.RETEST_REQUIRED, QCStatus.PASSED)

QC_FOR_DECISION = {
    DispositionDecision.RELEASED: QCStatus.PASSED,
    DispositionDecision.REJECTED: QCStatus.FAILED,
    DispositionDecision.ON_HOLD: QCStatus.ON_HOLD,
    DispositionDecision.RETEST: QCStatus.RETEST_REQUIRED,
}


def _require_role(user, roles, action):
    if not user.has_role(*roles):
        raise BusinessRuleViolation(f'Your role cannot {action}.', code='forbidden', status_code=403)


def _lock_sample(sample):
    return LabSample.objects.select_for_update(of=('self',)).select_related('material').get(pk=sample.pk)


def _audit_sample(user, action, sample, request=None, **kw):
    extra = kw.pop('extra', None) or {}
    record_audit(user, action, 'LabSample', sample.id, extra={'sample_id': sample.sample_id, **extra},
                 request=request, **kw)


def _set_qc(packages, qc_status, user, *, reason, sample, request=None, action=AuditEvent.ACTION_TRANSITION):
    """Change the quality dimension of locked packages, auditing each change on the package."""
    for pkg in packages:
        if pkg.qc_status == qc_status:
            continue
        before = pkg.qc_status
        pkg.qc_status = qc_status
        pkg.updated_by = user
        pkg.save(update_fields=['qc_status', 'updated_by', 'updated_at'])
        record_audit(user, action, 'Package', pkg.id, reason=reason,
                     before={'qc_status': before}, after={'qc_status': qc_status},
                     extra={'sample': str(sample.id), 'sample_id': sample.sample_id}, request=request)


def _covered_packages(sample, *, lock=True):
    """
    Packages a sample's result applies to: the explicitly covered packages plus any
    child packages later repacked from them (same physical material). Nothing else.
    """
    ids = set(sample.sample_packages.values_list('package_id', flat=True))
    frontier = set(ids)
    while frontier:
        children = set(Package.objects.filter(parent_package_id__in=frontier).values_list('pk', flat=True))
        frontier = children - ids
        ids |= children
    qs = Package.objects.filter(pk__in=ids).order_by('package_id')
    return list(qs.select_for_update()) if lock else list(qs)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------

def default_tests(material, packages):
    """Tests configured on the material, plus any the invoice lines asked for."""
    from apps.master.models import LabTestDefinition
    ids = set(material.test_requirements.values_list('test_definition_id', flat=True))
    for pkg in packages:
        ids |= set(pkg.invoice_line.required_tests.values_list('pk', flat=True))
    return list(LabTestDefinition.objects.filter(pk__in=ids, is_active=True))


@transaction.atomic
def create_sample(data, user, *, request=None):
    """
    Request a sample covering an explicit set of packages. The covered set is exactly
    what the eventual result applies to — no other packages are ever approved by it.
    """
    _require_role(user, LAB_ROLES, 'request samples')
    package_ids = [p.pk for p in data['packages']]
    if not package_ids:
        raise BusinessRuleViolation('Select at least one package.', details={'packages': ['Required.']})
    packages = list(Package.objects.select_for_update(of=('self',))
                    .select_related('material', 'invoice_line').filter(pk__in=package_ids).order_by('package_id'))
    material = data.get('material') or packages[0].material

    problems = []
    for pkg in packages:
        if pkg.material_id != material.pk:
            problems.append(f'{pkg.package_id}: different material ({pkg.material.code})')
        if pkg.status not in IN_WAREHOUSE_STATUSES or pkg.current_quantity <= 0:
            problems.append(f'{pkg.package_id}: not in stock ({pkg.get_status_display()})')
        if pkg.qc_status not in SAMPLEABLE_QC:
            problems.append(f'{pkg.package_id}: QC status {pkg.get_qc_status_display()} cannot be sampled')
        if pkg.is_recalled:
            problems.append(f'{pkg.package_id}: recalled')
    active = SamplePackage.objects.filter(package_id__in=package_ids, sample__status__in=ACTIVE_SAMPLE_STATUSES) \
        .select_related('sample', 'package')
    problems += [f'{sp.package.package_id}: already covered by active sample {sp.sample.sample_id}' for sp in active]
    scope = get_config('lab.sample_scope')
    if scope == 'SINGLE' and len(packages) > 1:
        problems.append('One sample covers one package (bag); request a separate sample for each package.')
    if scope == 'LOT':
        lots = {(p.batch_number or f'line:{p.invoice_line_id}') for p in packages}
        if len(lots) > 1:
            problems.append('Packages span more than one lot; a sample may cover a single lot only.')
    if problems:
        raise BusinessRuleViolation('These packages cannot be covered by one sample.', code='invalid_sample_scope',
                                    details={'packages': problems})

    tests = list(data.get('requested_tests') or default_tests(material, packages))
    if not tests:
        raise BusinessRuleViolation(
            f'No tests are configured for {material.code}. Select tests, or mark the material as not requiring QC.',
            code='no_tests')

    batches = {p.batch_number for p in packages}
    sample = LabSample.objects.create(
        material=material, batch_number=batches.pop() if len(batches) == 1 else '',
        laboratory_name=data.get('laboratory_name', ''), analyst_name=data.get('analyst_name', ''),
        specification=data.get('specification', ''), notes=data.get('notes', ''),
        required_by=data.get('required_by'), created_by=user, updated_by=user,
    )
    sample.requested_tests.set(tests)
    SamplePackage.objects.bulk_create([SamplePackage(sample=sample, package=p) for p in packages])
    _audit_sample(user, AuditEvent.ACTION_CREATE, sample, request,
                  after={'packages': [p.package_id for p in packages], 'tests': [t.code for t in tests]})
    notify_roles(('LAB_TECH',), title=f'Sample {sample.sample_id} awaiting collection',
                 message=f'{len(packages)} package(s) of {material.code} {sample.batch_number}'.strip(),
                 entity_type='LabSample', entity_id=sample.id, link_url=f'/lab/samples/{sample.id}', exclude=user)
    return sample


@transaction.atomic
def collect_sample(sample, data, user, *, request=None):
    """
    Record physical collection. Quantities drawn from packages leave stock through
    the ledger, so sampled quantity reconciles with inventory.
    """
    _require_role(user, LAB_ROLES, 'collect samples')
    sample = _lock_sample(sample)
    if sample.status != SampleStatus.REQUESTED:
        raise BusinessRuleViolation(f'Sample is {sample.get_status_display()}; only requested samples can be collected.')
    covered = {sp.package_id: sp for sp in sample.sample_packages.select_related('package')}
    draws = data.get('draws') or []
    total, units = Decimal('0'), set()
    for draw in draws:
        sp = covered.get(draw['package'].pk)
        if sp is None:
            raise BusinessRuleViolation(f"{draw['package'].package_id} is not covered by this sample.")
        pkg = Package.objects.select_for_update(of=('self',)).select_related('unit', 'current_location').get(pk=sp.package_id)
        qty = draw['quantity']
        if qty <= 0:
            raise BusinessRuleViolation('Drawn quantities must be greater than zero.')
        apply_quantity_change(pkg, -qty, reason=MovementReason.SAMPLE, user=user,
                              reference_id=sample.sample_id, notes='Laboratory sample')
        sp.drawn_quantity = (sp.drawn_quantity or 0) + qty
        sp.save(update_fields=['drawn_quantity', 'updated_at'])
        total += qty
        units.add(pkg.unit_id)

    sample.sampled_at = data.get('sampled_at') or timezone.now()
    sample.sampled_by = user
    sample.sample_condition = data.get('sample_condition', '')
    if data.get('sampled_quantity') is not None:
        sample.sampled_quantity, sample.sampled_unit = data['sampled_quantity'], data.get('sampled_unit')
    elif draws and len(units) == 1:
        sample.sampled_quantity, sample.sampled_unit_id = total, units.pop()
    if sample.sampled_quantity is not None and sample.sampled_unit is None:
        raise BusinessRuleViolation('A unit is required for the sampled quantity.', details={'sampled_unit': ['Required.']})
    sample.status = SampleStatus.COLLECTED
    sample.updated_by = user
    sample.save()
    _set_qc(_covered_packages(sample), QCStatus.SAMPLED, user, reason=f'Sampled ({sample.sample_id})',
            sample=sample, request=request)
    _audit_sample(user, AuditEvent.ACTION_TRANSITION, sample, request, after={'status': sample.status},
                  extra={'drawn': {d['package'].package_id: str(d['quantity']) for d in draws}})
    return sample


@transaction.atomic
def receive_in_lab(sample, user, *, request=None):
    _require_role(user, LAB_ROLES, 'receive samples')
    sample = _lock_sample(sample)
    if sample.status != SampleStatus.COLLECTED:
        raise BusinessRuleViolation('Only collected samples can be received by the lab.')
    sample.status = SampleStatus.RECEIVED_BY_LAB
    sample.received_by_lab_at = timezone.now()
    sample.updated_by = user
    sample.save()
    _audit_sample(user, AuditEvent.ACTION_TRANSITION, sample, request, after={'status': sample.status})
    return sample


@transaction.atomic
def start_testing(sample, user, *, notes='', request=None):
    """Open a new test run. For a retest the earlier runs are kept, never deleted."""
    _require_role(user, LAB_ROLES, 'start testing')
    sample = _lock_sample(sample)
    allowed = (SampleStatus.COLLECTED, SampleStatus.RECEIVED_BY_LAB, SampleStatus.RETEST_REQUIRED)
    if sample.status not in allowed:
        raise BusinessRuleViolation(f'Testing cannot start while the sample is {sample.get_status_display()}.')
    previous = sample.test_runs.order_by('-run_number').first()
    sample.test_runs.update(is_current=False)
    run = LabTestRun.objects.create(
        sample=sample, run_number=(previous.run_number + 1) if previous else 1, started_at=timezone.now(),
        analyst=user, notes=notes, created_by=user, updated_by=user,
    )
    sample.status = SampleStatus.TESTING
    sample.updated_by = user
    sample.save()
    _set_qc(_covered_packages(sample), QCStatus.TESTING, user, reason=f'Testing run {run.run_number} ({sample.sample_id})',
            sample=sample, request=request)
    _audit_sample(user, AuditEvent.ACTION_TRANSITION, sample, request, after={'status': sample.status},
                  extra={'run': run.run_number, 'retest': previous is not None})
    return run


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def evaluate(definition, entry):
    """
    Interpret a result against the definition's acceptance criteria.
    Returns the fields to store; raises on missing / inconsistent input.
    """
    out = {'min_limit': definition.min_value, 'max_limit': definition.max_value,
           'acceptable_values': list(definition.acceptable_text_values or []),
           'result_unit': entry.get('result_unit') or definition.unit,
           'numeric_result': None, 'text_result': '', 'boolean_result': None,
           'interpretation': entry.get('interpretation', '') or ''}
    if definition.unit_id and out['result_unit'] and out['result_unit'].pk != definition.unit_id:
        raise BusinessRuleViolation(f'{definition.code} must be reported in {definition.unit.symbol}.')
    kind = definition.result_type
    if kind == 'NUMERIC':
        value = entry.get('numeric_result')
        if value is None:
            raise BusinessRuleViolation(f'{definition.code} needs a numeric result.')
        out['numeric_result'] = value
        ok = (definition.min_value is None or value >= definition.min_value) and \
             (definition.max_value is None or value <= definition.max_value)
        out['result_status'] = TestResultStatus.PASS if ok else TestResultStatus.FAIL
    elif kind == 'BOOLEAN':
        value = entry.get('boolean_result')
        if value is None:
            raise BusinessRuleViolation(f'{definition.code} needs a pass/fail result.')
        out['boolean_result'] = value
        out['result_status'] = TestResultStatus.PASS if value else TestResultStatus.FAIL
    else:
        value = (entry.get('text_result') or '').strip()
        if not value:
            raise BusinessRuleViolation(f'{definition.code} needs a text result.')
        out['text_result'] = value
        if out['acceptable_values']:
            ok = value.lower() in {v.lower() for v in out['acceptable_values']}
            out['result_status'] = TestResultStatus.PASS if ok else TestResultStatus.FAIL
        else:
            # No automatic criterion: the analyst must state the interpretation
            if entry.get('result_status') not in TestResultStatus.values or not out['interpretation']:
                raise BusinessRuleViolation(
                    f'{definition.code} has no acceptance criterion; give result_status and an interpretation.')
            out['result_status'] = entry['result_status']
    return out


def _result_snapshot(r):
    return {'test': r.test_definition.code, 'numeric': str(r.numeric_result) if r.numeric_result is not None else None,
            'text': r.text_result, 'boolean': r.boolean_result, 'status': r.result_status, 'revision': r.revision}


@transaction.atomic
def record_results(run, entries, user, *, request=None):
    """Enter or correct results while a run is open. Every revision is audited."""
    _require_role(user, LAB_ROLES, 'enter results')
    run = LabTestRun.objects.select_for_update(of=('self',)).select_related('sample').get(pk=run.pk)
    if run.status != TestRunStatus.OPEN or not run.is_current:
        raise BusinessRuleViolation('Results can only be entered on the current, open test run.', code='run_locked')
    requested = {t.pk: t for t in run.sample.requested_tests.select_related('unit')}
    saved = []
    for entry in entries:
        definition = entry['test_definition']
        if definition.pk not in requested:
            raise BusinessRuleViolation(f'{definition.code} was not requested for this sample.')
        fields = evaluate(requested[definition.pk], entry)
        fields['remarks'] = entry.get('remarks', '') or ''
        existing = LabTestResult.objects.filter(test_run=run, test_definition=definition).first()
        if existing:
            before = _result_snapshot(existing)
            for k, v in fields.items():
                setattr(existing, k, v)
            existing.revision += 1
            existing.entered_by = user
            existing.updated_by = user
            existing.save()
            result, action = existing, AuditEvent.ACTION_UPDATE
        else:
            before = None
            result = LabTestResult.objects.create(test_run=run, test_definition=definition, entered_by=user,
                                                  created_by=user, updated_by=user, **fields)
            action = AuditEvent.ACTION_CREATE
        record_audit(user, action, 'LabTestResult', result.id, before=before, after=_result_snapshot(result),
                     extra={'sample': str(run.sample_id), 'run': run.run_number}, request=request)
        saved.append(result)
    return saved


def missing_required_tests(run):
    entered = set(run.results.values_list('test_definition_id', flat=True))
    return [t for t in run.sample.requested_tests.all() if t.is_required and t.pk not in entered]


@transaction.atomic
def submit_run(run, user, *, request=None):
    _require_role(user, LAB_ROLES, 'submit results')
    run = LabTestRun.objects.select_for_update(of=('self',)).select_related('sample').get(pk=run.pk)
    if run.status != TestRunStatus.OPEN or not run.is_current:
        raise BusinessRuleViolation('Only the current open run can be submitted.')
    missing = missing_required_tests(run)
    if missing:
        raise BusinessRuleViolation('Required results are missing.', code='results_incomplete',
                                    details={'missing_tests': [t.code for t in missing]})
    run.status = TestRunStatus.SUBMITTED
    run.submitted_at = timezone.now()
    run.submitted_by = user
    run.updated_by = user
    run.save()
    sample = _lock_sample(run.sample)
    sample.status = SampleStatus.RESULTS_SUBMITTED
    sample.updated_by = user
    sample.save()
    outcome = run_outcome(run)
    _audit_sample(user, AuditEvent.ACTION_TRANSITION, sample, request, after={'status': sample.status},
                  extra={'run': run.run_number, 'outcome': outcome})
    notify_roles(('LAB_REVIEWER',), title=f'Results awaiting review: {sample.sample_id}',
                 message=f'Run {run.run_number} outcome: {outcome}', level='WARNING' if outcome != 'PASS' else 'INFO',
                 entity_type='LabSample', entity_id=sample.id, link_url=f'/lab/samples/{sample.id}', exclude=user)
    return run


def run_outcome(run):
    statuses = set(run.results.values_list('result_status', flat=True))
    if not statuses:
        return 'NO_RESULTS'
    if TestResultStatus.FAIL in statuses:
        return 'FAIL'
    if TestResultStatus.INCONCLUSIVE in statuses:
        return 'INCONCLUSIVE'
    return 'PASS'


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------

@transaction.atomic
def decide(sample, data, user, *, request=None):
    """
    Record the quality disposition for a sample with submitted results.
    The backend QC status set here is authoritative for release.
    """
    _require_role(user, REVIEWER_ROLES, 'record quality dispositions')
    decision = data['decision']
    reason = (data.get('reason') or '').strip()
    if not reason:
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    sample = _lock_sample(sample)
    if sample.status != SampleStatus.RESULTS_SUBMITTED:
        raise BusinessRuleViolation(f'A disposition needs submitted results; sample is {sample.get_status_display()}.',
                                    code='results_not_submitted')
    run = LabTestRun.objects.select_for_update(of=('self',)).get(sample=sample, is_current=True)

    if get_config('lab.separation_of_duties'):
        involved = set(run.results.values_list('entered_by_id', flat=True)) | {run.submitted_by_id}
        if user.pk in involved:
            raise BusinessRuleViolation('Results cannot be approved by someone who entered or submitted them.',
                                        code='separation_of_duties', status_code=403)

    if decision in (DispositionDecision.RELEASED, DispositionDecision.PARTIAL):
        blocking = Q(test_definition__is_required=True)
        if get_config('lab.optional_failures_block_release'):
            blocking |= Q(result_status=TestResultStatus.FAIL)
        failing = list(run.results.exclude(result_status=TestResultStatus.PASS).filter(blocking)
                       .values_list('test_definition__code', 'result_status'))
        if failing or missing_required_tests(run):
            raise BusinessRuleViolation(
                'Material cannot be released while required results are failing, inconclusive or missing.',
                code='results_not_passing', details={'results': [f'{c}: {s}' for c, s in failing]})

    packages = _covered_packages(sample)
    released, remainder_decision = [], ''
    if decision == DispositionDecision.PARTIAL:
        chosen = [str(p.pk) for p in data.get('released_packages') or []]
        explicit = {str(i) for i in sample.sample_packages.values_list('package_id', flat=True)}
        if not chosen or not set(chosen) <= explicit or set(chosen) == explicit:
            raise BusinessRuleViolation('Partial acceptance needs a non-empty, proper subset of the sample packages.',
                                        details={'released_packages': ['Invalid selection.']})
        remainder_decision = data.get('remainder_decision') or DispositionDecision.REJECTED
        if remainder_decision not in (DispositionDecision.REJECTED, DispositionDecision.ON_HOLD):
            raise BusinessRuleViolation('Remainder must be REJECTED or ON_HOLD.')
        chosen_set = set(chosen)
        # Child packages follow their parent's decision
        for p in packages:
            root = p
            while str(root.pk) not in explicit and root.parent_package_id:
                root = root.parent_package
            if str(root.pk) in chosen_set:
                released.append(p)
        rest = [p for p in packages if p not in released]
        _set_qc(released, QCStatus.PASSED, user, reason=reason, sample=sample, request=request,
                action=AuditEvent.ACTION_APPROVE)
        _set_qc(rest, QC_FOR_DECISION[remainder_decision], user, reason=reason, sample=sample, request=request,
                action=AuditEvent.ACTION_REJECT)
    else:
        target = QC_FOR_DECISION[decision]
        _set_qc(packages, target, user, reason=reason, sample=sample, request=request,
                action=AuditEvent.ACTION_APPROVE if decision == DispositionDecision.RELEASED else AuditEvent.ACTION_REJECT)
        if decision == DispositionDecision.RELEASED:
            released = packages

    today = timezone.localdate()
    new_retest, new_expiry = data.get('new_retest_date'), data.get('new_expiry_date')
    for value, label in ((new_retest, 'retest'), (new_expiry, 'expiry')):
        if value and value <= today:
            raise BusinessRuleViolation(f'The new {label} date must be in the future.',
                                        details={f'new_{label}_date': ['Must be in the future.']})
        if value and not released:
            raise BusinessRuleViolation(f'A new {label} date can only be set on released packages.')
    # Released after retest: material past its dates is usable again only with new dates.
    stale = [p.package_id for p in released if p.expiry_date and p.expiry_date <= today]
    if stale and not new_expiry:
        raise BusinessRuleViolation(f'{", ".join(stale)} expired; give a new expiry date to release it for use.',
                                    code='new_expiry_required', details={'new_expiry_date': ['Required.']})
    overdue = [p.package_id for p in released if p.retest_date and p.retest_date < today]
    if overdue and not new_retest:
        raise BusinessRuleViolation(f'{", ".join(overdue)} is past its retest date; give the next retest date.',
                                    code='new_retest_required', details={'new_retest_date': ['Required.']})
    if new_retest or new_expiry:
        dates = {k: v for k, v in (('retest_date', new_retest), ('expiry_date', new_expiry)) if v}
        for p in released:
            before = {k: str(getattr(p, k)) if getattr(p, k) else None for k in dates}
            Package.objects.filter(pk=p.pk).update(**dates)
            record_audit(user, AuditEvent.ACTION_UPDATE, 'Package', p.id, reason=reason, before=before,
                         after={k: str(v) for k, v in dates.items()},
                         extra={'sample': sample.sample_id, 'kind': 'DATES_EXTENDED'}, request=request)

    disposition = QualityDisposition.objects.create(
        sample=sample, test_run=run, decision=decision, decided_by=user, decided_at=timezone.now(),
        reason=reason, notes=data.get('notes', ''), remainder_decision=remainder_decision,
        released_package_ids=[p.package_id for p in released],
        affected_package_ids=[p.package_id for p in packages],
        new_retest_date=new_retest, new_expiry_date=new_expiry, created_by=user, updated_by=user,
    )
    run.status = TestRunStatus.REVIEWED
    run.reviewed_at, run.reviewed_by, run.updated_by = timezone.now(), user, user
    run.save()
    if decision == DispositionDecision.RETEST:
        sample.status = SampleStatus.RETEST_REQUIRED
    elif decision == DispositionDecision.ON_HOLD:
        sample.status = SampleStatus.RESULTS_SUBMITTED  # awaiting a final decision
    else:
        sample.status = SampleStatus.COMPLETED
        sample.completed_at = timezone.now()
    sample.updated_by = user
    sample.save()

    record_audit(user, AuditEvent.ACTION_APPROVE if decision == DispositionDecision.RELEASED else AuditEvent.ACTION_REJECT,
                 'QualityDisposition', disposition.id, reason=reason,
                 after={'decision': decision, 'released': disposition.released_package_ids,
                        'affected': disposition.affected_package_ids, 'remainder': remainder_decision},
                 extra={'sample': str(sample.id), 'sample_id': sample.sample_id, 'run': run.run_number},
                 request=request)
    if decision != DispositionDecision.RELEASED:
        notify_roles(('WAREHOUSE', 'MANAGER', 'LAB_REVIEWER'),
                     title=f'{sample.sample_id}: {disposition.get_decision_display()}',
                     message=f'{sample.material.code} {sample.batch_number} — {reason}',
                     level='ERROR' if decision in (DispositionDecision.REJECTED, DispositionDecision.PARTIAL) else 'WARNING',
                     entity_type='LabSample', entity_id=sample.id, link_url=f'/lab/samples/{sample.id}', exclude=user)
    return disposition


@transaction.atomic
def cancel_sample(sample, user, *, reason, request=None):
    """Cancel before results exist. Covered packages return to pending sampling (drawn quantity stays consumed)."""
    _require_role(user, LAB_ROLES, 'cancel samples')
    if not (reason or '').strip():
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    sample = _lock_sample(sample)
    if sample.status not in (SampleStatus.REQUESTED, SampleStatus.COLLECTED, SampleStatus.RECEIVED_BY_LAB):
        raise BusinessRuleViolation('Only samples without test runs can be cancelled.')
    sample.status = SampleStatus.CANCELLED
    sample.cancel_reason = reason.strip()
    sample.updated_by = user
    sample.save()
    in_progress = [p for p in _covered_packages(sample) if p.qc_status in (QCStatus.SAMPLED, QCStatus.TESTING)]
    _set_qc(in_progress, QCStatus.PENDING_SAMPLING, user, reason=f'Sample {sample.sample_id} cancelled',
            sample=sample, request=request)
    _audit_sample(user, AuditEvent.ACTION_TRANSITION, sample, request, reason=reason.strip(),
                  after={'status': sample.status})
    return sample


@transaction.atomic
def attach_report(sample, uploaded_file, document_type, user, *, notes='', request=None):
    _require_role(user, LAB_ROLES, 'attach lab reports')
    mime = validate_upload(uploaded_file)
    attachment = LabSampleAttachment.objects.create(
        sample=sample, file=uploaded_file, original_filename=uploaded_file.name[:255],
        file_size_bytes=uploaded_file.size, mime_type=mime, document_type=document_type,
        notes=notes, uploaded_by=user,
    )
    _audit_sample(user, AuditEvent.ACTION_UPLOAD, sample, request,
                  after={'filename': attachment.original_filename, 'type': document_type},
                  extra={'attachment': str(attachment.id)})
    return attachment


def sample_timeline(sample):
    result_ids = [str(i) for i in LabTestResult.objects.filter(test_run__sample=sample).values_list('pk', flat=True)]
    return AuditEvent.objects.filter(
        Q(entity_type='LabSample', entity_id=str(sample.id))
        | Q(entity_type='LabTestResult', entity_id__in=result_ids)
        | Q(entity_type='QualityDisposition', extra__sample=str(sample.id))
    ).select_related('actor').order_by('timestamp')


def packages_awaiting_sampling():
    """In-stock packages needing a sample that are not yet covered by an active one."""
    active = SamplePackage.objects.filter(sample__status__in=ACTIVE_SAMPLE_STATUSES).values('package_id')
    return Package.objects.in_warehouse().filter(
        qc_status__in=(QCStatus.PENDING_SAMPLING, QCStatus.RETEST_REQUIRED), is_recalled=False,
    ).exclude(pk__in=active)


# ---------------------------------------------------------------------------
# Holds and recalls
# ---------------------------------------------------------------------------

FLAG = {HoldType.HOLD: 'is_on_hold', HoldType.RECALL: 'is_recalled'}


@transaction.atomic
def create_hold(data, user, *, request=None):
    """
    Place a quality hold or recall on explicit packages or on a whole lot
    (material + batch). Recalls also cover packages already issued, for traceability.
    """
    _require_role(user, REVIEWER_ROLES, 'place holds or recalls')
    reason = (data.get('reason') or '').strip()
    if not reason:
        raise BusinessRuleViolation('A reason is required.', details={'reason': ['Required.']})
    hold_type = data.get('hold_type') or HoldType.HOLD
    qs = Package.objects.none()
    if data.get('packages'):
        qs = Package.objects.filter(pk__in=[p.pk for p in data['packages']])
    elif data.get('material') and data.get('batch_number'):
        qs = Package.objects.filter(material=data['material'], batch_number__iexact=data['batch_number'])
        if hold_type == HoldType.HOLD:
            qs = qs.in_warehouse()
    else:
        raise BusinessRuleViolation('Select packages, or a material and batch number.')
    packages = list(qs.select_for_update().order_by('package_id'))
    if not packages:
        raise BusinessRuleViolation('No packages match this selection.')
    prefix = 'RCL' if hold_type == HoldType.RECALL else 'HLD'
    hold = QualityHold.objects.create(
        hold_type=hold_type, reference=generate_reference(prefix, 6), reason=reason,
        material=data.get('material'), batch_number=data.get('batch_number', ''),
        held_by=user, held_at=timezone.now(), created_by=user, updated_by=user,
    )
    hold.packages.set(packages)
    flag = FLAG[hold_type]
    for pkg in packages:
        if not getattr(pkg, flag):
            setattr(pkg, flag, True)
            pkg.updated_by = user
            pkg.save(update_fields=[flag, 'updated_by', 'updated_at'])
        record_audit(user, AuditEvent.ACTION_TRANSITION, 'Package', pkg.id, reason=reason,
                     after={flag: True}, extra={'hold': hold.reference}, request=request)
    record_audit(user, AuditEvent.ACTION_CREATE, 'QualityHold', hold.id, reason=reason,
                 after={'type': hold_type, 'packages': [p.package_id for p in packages]}, request=request)
    notify_roles(('WAREHOUSE', 'MANAGER', 'LAB_REVIEWER'), title=f'{hold.get_hold_type_display()} {hold.reference}',
                 message=f'{len(packages)} package(s): {reason}', level='ERROR' if hold_type == HoldType.RECALL else 'WARNING',
                 entity_type='QualityHold', entity_id=hold.id, link_url='/holds', exclude=user)
    return hold


@transaction.atomic
def release_hold(hold, user, *, notes, request=None):
    _require_role(user, REVIEWER_ROLES, 'release holds or recalls')
    if not (notes or '').strip():
        raise BusinessRuleViolation('Release notes are required.', details={'notes': ['Required.']})
    hold = QualityHold.objects.select_for_update().get(pk=hold.pk)
    if not hold.is_active:
        raise BusinessRuleViolation('This hold is already released.')
    hold.is_active = False
    hold.released_by, hold.released_at, hold.release_notes = user, timezone.now(), notes.strip()
    hold.updated_by = user
    hold.save()
    flag = FLAG[hold.hold_type]
    # A package stays flagged while another active hold of the same type still covers it
    still = QualityHold.objects.filter(is_active=True, hold_type=hold.hold_type).values('packages')
    clear = hold.packages.exclude(pk__in=still)
    for pkg in clear.select_for_update():
        setattr(pkg, flag, False)
        pkg.updated_by = user
        pkg.save(update_fields=[flag, 'updated_by', 'updated_at'])
        record_audit(user, AuditEvent.ACTION_TRANSITION, 'Package', pkg.id, reason=notes.strip(),
                     after={flag: False}, extra={'hold': hold.reference}, request=request)
    record_audit(user, AuditEvent.ACTION_UPDATE, 'QualityHold', hold.id, reason=notes.strip(),
                 after={'is_active': False}, request=request)
    return hold


def hold_impact(hold):
    """Forward traceability: where the held / recalled material is now, and what was already issued."""
    packages = hold.packages.select_related('current_location', 'unit')
    issued = MaterialIssueLine.objects.filter(package__in=hold.packages.all()).select_related(
        'issue__factory', 'package', 'unit')
    return {
        'in_stock': [{'package_id': p.package_id, 'quantity': str(p.current_quantity), 'unit': p.unit.symbol,
                      'location': str(p.current_location) if p.current_location_id else None, 'status': p.status}
                     for p in packages if p.status in IN_WAREHOUSE_STATUSES and p.current_quantity > 0],
        'issued': [{'issue_number': l.issue.issue_number, 'factory': l.issue.factory.name, 'package_id': l.package.package_id,
                    'quantity': str(l.issued_quantity), 'unit': l.unit.symbol, 'issued_at': l.created_at}
                   for l in issued],
    }
