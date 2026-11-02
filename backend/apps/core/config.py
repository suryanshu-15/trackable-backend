"""
Typed accessors for runtime settings stored in SystemConfiguration,
with documented defaults used when no row exists.
"""
import json
from decimal import Decimal

from apps.core.models import SystemConfiguration

DEFAULTS = {
    # Fields that together identify a duplicate supplier invoice.
    'receipt.duplicate_check_fields': ['supplier', 'invoice_number', 'invoice_date'],
    # Allowed % difference between gate net weight and sum of declared line weights
    # before a weight discrepancy alert is raised.
    'receipt.weight_tolerance_percent': '0.5',
    # The user who recorded a discrepant verification may not also accept it.
    'receiving.discrepancy_acceptance_separation': True,
    # WHOLE: an invoice line is either accepted in full (everything received) or rejected
    # (nothing accepted); a rejection needs authorised acceptance like any discrepancy.
    # PARTIAL: any accepted quantity up to received − damaged − mismatched.
    'receiving.line_acceptance': 'WHOLE',
    # Unit codes packages may be counted in (empty = any unit). Business rule: bags in tonnes.
    'package.unit_codes': ['T'],
    # Printed on labels.
    'package.type': 'BAG',
    # Upper bounds (days) of stock ageing buckets; a final open-ended bucket is added.
    'stock.ageing_buckets': [30, 60, 90],
    # Window for near-expiry / retest-due alerts.
    'stock.near_expiry_days': 30,
    # Whoever entered or submitted a run's results may not record its disposition.
    'lab.separation_of_duties': True,
    # SINGLE: one sample per package (bag); its result applies to that bag only (business rule).
    # LOT: a sample may cover several packages of one material and one batch/lot
    # (packages without a batch must come from the same invoice line).
    # PACKAGE: several packages of one material, no lot restriction.
    'lab.sample_scope': 'SINGLE',
    # Whether a failing *optional* test blocks release. Required tests always must pass.
    'lab.optional_failures_block_release': False,
    # AUTO: FEFO when expiry / retest dates apply to the material, otherwise FIFO. Or force 'FIFO' / 'FEFO'.
    'issue.allocation_policy': 'AUTO',
    # ALLOW: out-of-order picks need an override role + reason. FORBID: always refused.
    'issue.fifo_override_policy': 'ALLOW',
    'issue.override_roles': ['ADMIN', 'MANAGER'],
    # Only whole packages may be issued (overrides Material.allow_partial_issue).
    'issue.whole_packages_only': True,
    # Every issue must fulfil a material request (no free-standing issue slips).
    'issue.require_request': True,
    # Packages still waiting for put-away after this many hours raise an alert.
    'alerts.putaway_overdue_hours': 24,
    # Who may add master data from the forms that use it ("+ New" next to a dropdown).
    # Editing and deleting master data stay with administrators. Unlisted kinds: admin only.
    'master.create_roles': {
        'supplier': ['ADMIN', 'MANAGER', 'GATE'],
        'material': ['ADMIN', 'MANAGER', 'GATE', 'RECEIVING'],
        'unit': ['ADMIN', 'MANAGER'],
        'location': ['ADMIN', 'MANAGER', 'WAREHOUSE'],
        'factory': ['ADMIN', 'MANAGER'],
        'department': ['ADMIN', 'MANAGER', 'WAREHOUSE', 'FACTORY'],
        'test_definition': ['ADMIN', 'MANAGER', 'LAB_REVIEWER'],
        'category': ['ADMIN', 'MANAGER'],
    },
    # Retention (days) applied by `manage.py apply_retention`.
    'retention.audit_days': 365,
    'retention.document_days': 365,
    'retention.lab_days': 1095,
}

# Keys the frontend may read (GET /api/v1/policy/) to shape forms; the API still enforces them.
PUBLIC_KEYS = [
    'receiving.line_acceptance', 'package.unit_codes', 'package.type', 'lab.sample_scope',
    'lab.optional_failures_block_release', 'issue.whole_packages_only', 'issue.require_request',
    'issue.fifo_override_policy', 'issue.override_roles', 'stock.near_expiry_days', 'master.create_roles',
]


def get_config(key):
    row = SystemConfiguration.objects.filter(key=key).first()
    if row is None:
        return DEFAULTS.get(key)
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return row.value


def get_decimal(key):
    return Decimal(str(get_config(key)))
