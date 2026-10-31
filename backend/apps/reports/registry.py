"""
Report definitions (spec §12). Each report declares who may run it, which filters it
accepts and how a queryset row becomes columns. The same definition serves the JSON
view and the CSV / Excel exports, so exports always respect the UI's permissions
and filters.
"""
from dataclasses import dataclass, field
from typing import Callable

from django.db.models import Count, Q
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation
from apps.core.models import AuditEvent
from apps.inventory.models import (
    Package, MaterialIssueLine, StockAdjustment, MaterialReturn, DisposalRecord,
)
from apps.inventory import services as inv
from apps.laboratory.models import LabTestResult, QualityDisposition
from apps.master.models import Supplier
from apps.receiving.models import GoodsReceipt, InvoiceLine, VerificationStatus

ALL_OPERATIONAL = ('ADMIN', 'MANAGER', 'AUDITOR', 'WAREHOUSE', 'GATE', 'RECEIVING', 'LAB_TECH', 'LAB_REVIEWER')
OVERSIGHT = ('ADMIN', 'MANAGER', 'AUDITOR')


def _dt(value):
    return timezone.localtime(value).strftime('%Y-%m-%d %H:%M') if value else ''


def _d(value):
    return value.isoformat() if value else ''


def _n(value):
    return '' if value is None else str(value)


def _user(u):
    return u.display_name if u else ''


@dataclass
class Report:
    key: str
    title: str
    description: str
    roles: tuple
    columns: object                    # [(header, fn(row) -> value)], or a callable returning that list
    queryset: Callable                 # fn(params) -> iterable of rows
    filters: list = field(default_factory=list)

    def rows(self, params):
        return self.queryset(params)

    def get_columns(self):
        return self.columns() if callable(self.columns) else self.columns


def _date_range(params, qs, field_name):
    if params.get('date_from'):
        qs = qs.filter(**{f'{field_name}__date__gte': params['date_from']})
    if params.get('date_to'):
        qs = qs.filter(**{f'{field_name}__date__lte': params['date_to']})
    return qs


def _eq(params, qs, mapping):
    for param, lookup in mapping.items():
        value = params.get(param)
        if value not in (None, ''):
            qs = qs.filter(**{lookup: value})
    return qs


# --- Inward ----------------------------------------------------------------

def _inward(params):
    qs = GoodsReceipt.objects.select_related('supplier', 'weight_unit', 'created_by').annotate(
        line_count=Count('invoice_lines', distinct=True)).order_by('-arrival_datetime')
    qs = _date_range(params, qs, 'arrival_datetime')
    return _eq(params, qs, {'supplier': 'supplier_id', 'status': 'status'})


INWARD = Report(
    'inward-register', 'Inward register', 'Every gate receipt with weights and status.', ALL_OPERATIONAL,
    [('Receipt', lambda r: r.receipt_number), ('Arrival', lambda r: _dt(r.arrival_datetime)),
     ('Supplier', lambda r: r.supplier.name), ('Invoice', lambda r: r.invoice_number),
     ('Invoice date', lambda r: _d(r.invoice_date)), ('Vehicle', lambda r: r.vehicle_number),
     ('PO', lambda r: r.purchase_order_number), ('Gross', lambda r: _n(r.gross_weight)),
     ('Tare', lambda r: _n(r.tare_weight)), ('Net', lambda r: _n(r.net_weight)),
     ('Unit', lambda r: r.weight_unit.symbol if r.weight_unit_id else ''), ('Lines', lambda r: r.line_count),
     ('Status', lambda r: r.get_status_display()), ('Registered by', lambda r: _user(r.created_by))],
    _inward, ['date_from', 'date_to', 'supplier', 'status'],
)


def _supplier_summary(params):
    receipts = _date_range(params, GoodsReceipt.objects.exclude(status='CANCELLED'), 'arrival_datetime')
    lines = InvoiceLine.objects.filter(receipt__in=receipts)
    return Supplier.objects.filter(receipts__in=receipts).distinct().annotate(
        receipts_n=Count('receipts', filter=Q(receipts__in=receipts), distinct=True),
        lines_n=Count('receipts__invoice_lines', filter=Q(receipts__invoice_lines__in=lines), distinct=True),
        discrepant_n=Count('receipts__invoice_lines', distinct=True, filter=Q(
            receipts__invoice_lines__in=lines,
            receipts__invoice_lines__verification__verification_status__in=[
                VerificationStatus.DISCREPANT, VerificationStatus.EXCEPTION_ACCEPTED])),
        accepted_n=Count('receipts__invoice_lines', distinct=True, filter=Q(
            receipts__invoice_lines__in=lines,
            receipts__invoice_lines__verification__verification_status=VerificationStatus.EXCEPTION_ACCEPTED)),
    ).order_by('name')


SUPPLIER = Report(
    'supplier-receipts', 'Supplier-wise receipts & discrepancies',
    'Receipts, invoice lines and discrepant lines per supplier.', ALL_OPERATIONAL,
    [('Supplier code', lambda s: s.code), ('Supplier', lambda s: s.name), ('Receipts', lambda s: s.receipts_n),
     ('Invoice lines', lambda s: s.lines_n), ('Discrepant lines', lambda s: s.discrepant_n),
     ('Exceptions accepted', lambda s: s.accepted_n),
     ('Discrepancy rate %', lambda s: f'{(s.discrepant_n / s.lines_n * 100):.1f}' if s.lines_n else '0.0')],
    _supplier_summary, ['date_from', 'date_to'],
)


def _reconciliation(params):
    qs = InvoiceLine.objects.select_related(
        'receipt__supplier', 'material', 'declared_unit', 'verification__received_unit').order_by(
        '-receipt__arrival_datetime', 'line_number')
    qs = _date_range(params, qs, 'receipt__arrival_datetime')
    qs = _eq(params, qs, {'supplier': 'receipt__supplier_id', 'material': 'material_id',
                          'verification_status': 'verification__verification_status'})
    if params.get('discrepant_only') in ('true', '1'):
        qs = qs.filter(verification__verification_status__in=[VerificationStatus.DISCREPANT,
                                                              VerificationStatus.EXCEPTION_ACCEPTED])
    return qs


def _v(line, attr):
    v = getattr(line, 'verification', None)
    return _n(getattr(v, attr)) if v else ''


RECONCILIATION = Report(
    'receipt-reconciliation', 'Invoice vs actual receipt reconciliation',
    'Declared invoice quantities against what was physically received and accepted.', ALL_OPERATIONAL,
    [('Receipt', lambda l: l.receipt.receipt_number), ('Arrival', lambda l: _dt(l.receipt.arrival_datetime)),
     ('Supplier', lambda l: l.receipt.supplier.name), ('Invoice', lambda l: l.receipt.invoice_number),
     ('Line', lambda l: l.line_number), ('Material', lambda l: l.material.code if l.material_id else l.material_name_declared),
     ('Declared', lambda l: _n(l.declared_quantity)), ('Declared unit', lambda l: l.declared_unit.symbol),
     ('Received', lambda l: _v(l, 'received_quantity')),
     ('Received unit', lambda l: l.verification.received_unit.symbol if hasattr(l, 'verification') and l.verification.received_unit_id else ''),
     ('Accepted', lambda l: _v(l, 'accepted_quantity')), ('Short', lambda l: _v(l, 'short_quantity')),
     ('Excess', lambda l: _v(l, 'excess_quantity')), ('Damaged', lambda l: _v(l, 'damaged_quantity')),
     ('Mismatched', lambda l: _v(l, 'mismatched_quantity')),
     ('Packaged', lambda l: _n(inv.packaged_quantity(l))),
     ('Verification', lambda l: l.verification.get_verification_status_display() if hasattr(l, 'verification') else 'Not verified'),
     ('Discrepancy', lambda l: l.verification.get_discrepancy_status_display() if hasattr(l, 'verification') else '')],
    _reconciliation, ['date_from', 'date_to', 'supplier', 'material', 'verification_status', 'discrepant_only'],
)


# --- Stock -----------------------------------------------------------------

def _reserved(pkg):
    return sum((r.reserved_quantity for r in pkg.reservations.all() if r.is_active), 0)


def _stock(params):
    qs = Package.objects.in_warehouse().select_related(
        'material', 'unit', 'current_location__warehouse', 'supplier', 'invoice_line__receipt').prefetch_related(
        'reservations').order_by('material__code', 'received_at', 'package_id')
    qs = _eq(params, qs, {'material': 'material_id', 'location': 'current_location_id', 'qc_status': 'qc_status',
                          'warehouse': 'current_location__warehouse_id', 'batch_number': 'batch_number__iexact',
                          'supplier': 'supplier_id'})
    if params.get('available') in ('true', '1'):
        qs = qs.filter(pk__in=Package.objects.available().values('pk'))
    return qs


STOCK = Report(
    'current-stock', 'Current stock', 'Every package in the warehouse with location, quality and availability.',
    ALL_OPERATIONAL,
    [('Package', lambda p: p.package_id), ('Material', lambda p: p.material.code), ('Material name', lambda p: p.material.name),
     ('Lot', lambda p: p.batch_number), ('Quantity', lambda p: _n(p.current_quantity)), ('Unit', lambda p: p.unit.symbol),
     ('Reserved', lambda p: _n(_reserved(p))),
     ('Warehouse', lambda p: p.current_location.warehouse.code if p.current_location_id else ''),
     ('Location', lambda p: p.current_location.code if p.current_location_id else ''),
     ('Status', lambda p: p.get_status_display()), ('QC status', lambda p: p.get_qc_status_display()),
     ('On hold', lambda p: 'Yes' if p.is_on_hold else ''), ('Recalled', lambda p: 'Yes' if p.is_recalled else ''),
     ('Available', lambda p: 'Yes' if p.is_available else 'No'),
     ('Received', lambda p: _d(timezone.localtime(p.received_at).date())),
     ('Age (days)', lambda p: (timezone.localdate() - timezone.localtime(p.received_at).date()).days),
     ('Expiry', lambda p: _d(p.expiry_date)), ('Retest', lambda p: _d(p.retest_date)),
     ('Supplier', lambda p: p.supplier.name), ('Receipt', lambda p: p.invoice_line.receipt.receipt_number)],
    _stock, ['material', 'location', 'warehouse', 'qc_status', 'batch_number', 'supplier', 'available'],
)


def _ageing_columns():
    cols = [('Material', lambda r: r['material_code']), ('Material name', lambda r: r['material_name']),
            ('Unit', lambda r: r['unit'])]
    return cols + [(f'{label} days', (lambda b: lambda r: r['quantities'][b])(label))
                   for label, _, _ in inv.ageing_buckets()]


AGEING = Report('stock-ageing', 'Stock ageing', 'Quantity per material in configurable age buckets (by received date).',
                ALL_OPERATIONAL, _ageing_columns, lambda params: inv.stock_ageing(_stock(params))['rows'],
                ['material', 'warehouse', 'location'])


def _expiry(params):
    return inv.expiry_exposure(_stock(params), params.get('days')).select_related(
        'material', 'unit', 'current_location', 'invoice_line__receipt')


EXPIRY = Report(
    'expiry-exposure', 'Expiry & retest exposure', 'Stock expired, due for retest, or becoming so within the window.',
    ALL_OPERATIONAL,
    [('Package', lambda p: p.package_id), ('Material', lambda p: p.material.code), ('Lot', lambda p: p.batch_number),
     ('Quantity', lambda p: _n(p.current_quantity)), ('Unit', lambda p: p.unit.symbol),
     ('Expiry', lambda p: _d(p.expiry_date)), ('Retest', lambda p: _d(p.retest_date)),
     ('Days to expiry', lambda p: (p.expiry_date - timezone.localdate()).days if p.expiry_date else ''),
     ('Location', lambda p: p.current_location.code if p.current_location_id else ''),
     ('QC status', lambda p: p.get_qc_status_display())],
    _expiry, ['days', 'material', 'warehouse'],
)


# --- Laboratory ------------------------------------------------------------

def _lab_register(params):
    qs = LabTestResult.objects.select_related(
        'test_run__sample__material', 'test_definition', 'result_unit', 'entered_by').order_by(
        '-test_run__sample__created_at', 'test_run__run_number', 'test_definition__code')
    qs = _date_range(params, qs, 'entered_at')
    return _eq(params, qs, {'material': 'test_run__sample__material_id', 'result_status': 'result_status',
                            'test': 'test_definition_id', 'batch_number': 'test_run__sample__batch_number__iexact'})


def _value(r):
    if r.numeric_result is not None:
        return _n(r.numeric_result.normalize())
    if r.boolean_result is not None:
        return 'Pass' if r.boolean_result else 'Fail'
    return r.text_result


LAB = Report(
    'lab-register', 'Laboratory test register', 'Every test result with limits, outcome and analyst.',
    ('ADMIN', 'MANAGER', 'AUDITOR', 'LAB_TECH', 'LAB_REVIEWER', 'WAREHOUSE'),
    [('Sample', lambda r: r.test_run.sample.sample_id), ('Material', lambda r: r.test_run.sample.material.code),
     ('Lot', lambda r: r.test_run.sample.batch_number), ('Run', lambda r: r.test_run.run_number),
     ('Test', lambda r: r.test_definition.code), ('Result', _value),
     ('Unit', lambda r: r.result_unit.symbol if r.result_unit_id else ''),
     ('Min', lambda r: _n(r.min_limit.normalize()) if r.min_limit is not None else ''),
     ('Max', lambda r: _n(r.max_limit.normalize()) if r.max_limit is not None else ''),
     ('Outcome', lambda r: r.get_result_status_display()), ('Revision', lambda r: r.revision),
     ('Entered by', lambda r: _user(r.entered_by)), ('Entered at', lambda r: _dt(r.entered_at)),
     ('Run status', lambda r: r.test_run.get_status_display())],
    _lab_register, ['date_from', 'date_to', 'material', 'result_status', 'test', 'batch_number'],
)


def _decisions(params):
    qs = QualityDisposition.objects.select_related('sample__material', 'decided_by', 'test_run').order_by('-decided_at')
    qs = _date_range(params, qs, 'decided_at')
    return _eq(params, qs, {'decision': 'decision', 'material': 'sample__material_id'})


DECISIONS = Report(
    'qc-decisions', 'QC decisions (pass / fail history)', 'Every quality disposition with the packages it affected.',
    ('ADMIN', 'MANAGER', 'AUDITOR', 'LAB_TECH', 'LAB_REVIEWER', 'WAREHOUSE'),
    [('Sample', lambda d: d.sample.sample_id), ('Material', lambda d: d.sample.material.code),
     ('Lot', lambda d: d.sample.batch_number), ('Run', lambda d: d.test_run.run_number if d.test_run_id else ''),
     ('Decision', lambda d: d.get_decision_display()), ('Decided by', lambda d: _user(d.decided_by)),
     ('Decided at', lambda d: _dt(d.decided_at)), ('Reason', lambda d: d.reason),
     ('Released packages', lambda d: ', '.join(d.released_package_ids)),
     ('Affected packages', lambda d: len(d.affected_package_ids))],
    _decisions, ['date_from', 'date_to', 'decision', 'material'],
)


# --- Issue & stage 9 -------------------------------------------------------

def _issues(params):
    qs = MaterialIssueLine.objects.select_related(
        'issue__factory', 'issue__department', 'issue__material_request', 'issue__issued_by', 'issue__received_by',
        'package', 'material', 'unit').order_by('-issue__issue_datetime', 'package__package_id')
    qs = _date_range(params, qs, 'issue__issue_datetime')
    qs = _eq(params, qs, {'factory': 'issue__factory_id', 'material': 'material_id',
                          'production_order': 'issue__production_order__iexact'})
    if params.get('overrides_only') in ('true', '1'):
        qs = qs.filter(issue__fifo_override=True)
    return qs


ISSUES = Report(
    'issue-register', 'Material issue & consumption register', 'Every package issued to a factory.',
    ('ADMIN', 'MANAGER', 'AUDITOR', 'WAREHOUSE'),
    [('Issue', lambda l: l.issue.issue_number), ('Issued at', lambda l: _dt(l.issue.issue_datetime)),
     ('Factory', lambda l: l.issue.factory.name), ('Department', lambda l: l.issue.department.name if l.issue.department_id else ''),
     ('Production order', lambda l: l.issue.production_order),
     ('Request', lambda l: l.issue.material_request.request_number if l.issue.material_request_id else ''),
     ('Package', lambda l: l.package.package_id), ('Material', lambda l: l.material.code), ('Lot', lambda l: l.batch_number),
     ('Quantity', lambda l: _n(l.issued_quantity)), ('Unit', lambda l: l.unit.symbol),
     ('QC at issue', lambda l: l.get_qc_status_at_issue_display()),
     ('FIFO/FEFO compliant', lambda l: 'Yes' if l.fifo_compliant else 'No (override)'),
     ('Override reason', lambda l: l.issue.fifo_override_reason), ('Issued by', lambda l: _user(l.issue.issued_by)),
     ('Received by', lambda l: _user(l.issue.received_by) or l.issue.received_by_name),
     ('Acknowledged', lambda l: _dt(l.issue.acknowledged_at))],
    _issues, ['date_from', 'date_to', 'factory', 'material', 'production_order', 'overrides_only'],
)


def _adjustments(params):
    qs = StockAdjustment.objects.select_related('package__material', 'unit', 'created_by', 'approved_by').order_by('-created_at')
    qs = _date_range(params, qs, 'created_at')
    return _eq(params, qs, {'status': 'status', 'adjustment_type': 'adjustment_type'})


ADJUSTMENTS = Report(
    'stock-adjustments', 'Stock adjustments', 'Requested and approved stock corrections.', ('ADMIN', 'MANAGER', 'AUDITOR', 'WAREHOUSE'),
    [('Requested at', lambda a: _dt(a.created_at)), ('Package', lambda a: a.package.package_id),
     ('Material', lambda a: a.package.material.code), ('Type', lambda a: a.get_adjustment_type_display()),
     ('Quantity', lambda a: _n(a.quantity_change)), ('Unit', lambda a: a.unit.symbol),
     ('Applied delta', lambda a: _n(a.applied_delta)), ('Status', lambda a: a.get_status_display()),
     ('Reason', lambda a: a.reason), ('Requested by', lambda a: _user(a.created_by)),
     ('Decided by', lambda a: _user(a.approved_by)), ('Decided at', lambda a: _dt(a.approved_at))],
    _adjustments, ['date_from', 'date_to', 'status', 'adjustment_type'],
)


def _returns(params):
    return _date_range(params, MaterialReturn.objects.select_related(
        'issue_line__issue__factory', 'issue_line__package', 'issue_line__material', 'return_package', 'unit',
        'received_by').order_by('-created_at'), 'created_at')


RETURNS = Report(
    'returns', 'Returns from factory', 'Material returned and the package created for it.', ('ADMIN', 'MANAGER', 'AUDITOR', 'WAREHOUSE'),
    [('Return', lambda r: r.return_number), ('Returned at', lambda r: _dt(r.created_at)),
     ('Issue', lambda r: r.issue_line.issue.issue_number), ('Factory', lambda r: r.issue_line.issue.factory.name),
     ('Material', lambda r: r.issue_line.material.code), ('Original package', lambda r: r.issue_line.package.package_id),
     ('Return package', lambda r: r.return_package.package_id if r.return_package_id else ''),
     ('Quantity', lambda r: _n(r.returned_quantity)), ('Unit', lambda r: r.unit.symbol),
     ('Requires QC', lambda r: 'Yes' if r.requires_qc else 'Inspection hold'), ('Reason', lambda r: r.reason),
     ('Received by', lambda r: _user(r.received_by))],
    _returns, ['date_from', 'date_to'],
)


def _disposals(params):
    qs = DisposalRecord.objects.select_related('package__material', 'unit', 'authorized_by').order_by('-created_at')
    return _eq(params, _date_range(params, qs, 'created_at'), {'disposal_type': 'disposal_type'})


DISPOSALS = Report(
    'disposals', 'Disposals & supplier returns', 'Stock scrapped or returned to the supplier.', ('ADMIN', 'MANAGER', 'AUDITOR', 'WAREHOUSE'),
    [('Number', lambda d: d.disposal_number), ('Type', lambda d: d.get_disposal_type_display()),
     ('Date', lambda d: _d(d.disposal_date)), ('Package', lambda d: d.package.package_id),
     ('Material', lambda d: d.package.material.code), ('Quantity', lambda d: _n(d.quantity)), ('Unit', lambda d: d.unit.symbol),
     ('Reason', lambda d: d.reason), ('Reference', lambda d: d.reference), ('Authorised by', lambda d: _user(d.authorized_by))],
    _disposals, ['date_from', 'date_to', 'disposal_type'],
)


def _audit(params):
    qs = AuditEvent.objects.select_related('actor').order_by('-timestamp')
    qs = _date_range(params, qs, 'timestamp')
    return _eq(params, qs, {'actor': 'actor_id', 'action': 'action', 'entity_type': 'entity_type', 'entity_id': 'entity_id'})


AUDIT = Report(
    'audit-events', 'Audit events', 'Immutable audit trail of sensitive actions.', OVERSIGHT,
    [('Time', lambda e: _dt(e.timestamp)), ('Actor', lambda e: _user(e.actor)), ('Action', lambda e: e.action),
     ('Entity', lambda e: e.entity_type), ('Entity ID', lambda e: e.entity_id), ('Reason', lambda e: e.reason),
     ('Before', lambda e: e.before_state or ''), ('After', lambda e: e.after_state or ''),
     ('IP', lambda e: e.ip_address or ''), ('Request ID', lambda e: e.correlation_id)],
    _audit, ['date_from', 'date_to', 'actor', 'action', 'entity_type', 'entity_id'],
)


def _lifecycle(params):
    code = params.get('package')
    if not code:
        raise BusinessRuleViolation('The package parameter (package ID) is required.', details={'package': ['Required.']})
    pkg = Package.objects.filter(package_id__iexact=code).first()
    if pkg is None:
        raise BusinessRuleViolation('Package not found.', code='not_found', status_code=404)
    return inv.package_timeline(pkg)


LIFECYCLE = Report(
    'package-lifecycle', 'Package lifecycle / traceability', 'Chronological history of one package, gate to today.',
    ALL_OPERATIONAL,
    [('Time', lambda e: _dt(e['timestamp'])), ('Event', lambda e: e['kind']), ('Description', lambda e: e['title']),
     ('By', lambda e: e['actor'] or ''), ('Details', lambda e: e['details'])],
    _lifecycle, ['package'],
)

REPORTS = {r.key: r for r in (INWARD, SUPPLIER, RECONCILIATION, STOCK, AGEING, EXPIRY, LAB, DECISIONS, ISSUES,
                              ADJUSTMENTS, RETURNS, DISPOSALS, AUDIT, LIFECYCLE)}
