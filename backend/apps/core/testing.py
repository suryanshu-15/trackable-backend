"""
Shared test fixtures. Test data only — never imported by application code.
"""
import json
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.models import SystemConfiguration
from apps.master.models import Supplier, UnitOfMeasure, UoMConversion, Material, Warehouse, StorageLocation
from apps.users.models import User, Role


def make_user(role, username=None, **extra):
    username = username or f'test_{role.lower()}'
    return User.objects.create_user(
        username=username, email=f'{username}@test.invalid', password='Test-pass-123!', role=role, **extra,
    )


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# The engine's configurable rules in their most general form. Most tests exercise this; the
# business settings (config DEFAULTS: whole-line acceptance, bags in tonnes, one bag per sample,
# whole-package issue against a request) are covered by apps/core/test_business_rules.py.
GENERIC_POLICY = {
    'receiving.line_acceptance': 'PARTIAL',
    'package.unit_codes': [],
    'lab.sample_scope': 'LOT',
    'lab.optional_failures_block_release': True,
    'issue.whole_packages_only': False,
    'issue.require_request': False,
}
GENERIC_MATERIAL = {'requires_expiry': False, 'requires_retest': False, 'allow_partial_issue': True}


def set_config(**values):
    """Set config keys for a test; keys use '__' for '.', e.g. set_config(issue__require_request=True)."""
    for key, value in values.items():
        SystemConfiguration.objects.update_or_create(key=key.replace('__', '.'), defaults={'value': json.dumps(value)})


class Fixtures:
    """Minimal master data most receiving / inventory tests need."""

    def __init__(self, policy=None):
        for key, value in (GENERIC_POLICY if policy is None else policy).items():
            SystemConfiguration.objects.update_or_create(key=key, defaults={'value': json.dumps(value)})
        self.kg = UnitOfMeasure.objects.create(code='KG', name='Kilogram', symbol='kg', unit_type='WEIGHT')
        self.pcs = UnitOfMeasure.objects.create(code='PCS', name='Pieces', symbol='pcs', unit_type='COUNT')
        self.supplier = Supplier.objects.create(code='SUP-1', name='Test Supplier One')
        self.supplier2 = Supplier.objects.create(code='SUP-2', name='Test Supplier Two')
        self.material = Material.objects.create(code='MAT-1', name='Test Material', default_unit=self.kg, **GENERIC_MATERIAL)
        self.tonne = UnitOfMeasure.objects.create(code='T', name='Tonne', symbol='t', unit_type='WEIGHT')
        UoMConversion.objects.create(from_unit=self.tonne, to_unit=self.kg, factor=Decimal('1000'))
        self.material_noqc = Material.objects.create(
            code='MAT-2', name='No-QC Material', default_unit=self.kg, qc_required=False, **GENERIC_MATERIAL)
        self.users = {role.value: make_user(role.value) for role in Role}
        self.warehouse = Warehouse.objects.create(code='WH', name='Warehouse')
        self.quarantine = StorageLocation.objects.create(
            warehouse=self.warehouse, code='Q1', name='Quarantine bin', location_type='QUARANTINE', is_quarantine=True)
        self.zone = StorageLocation.objects.create(warehouse=self.warehouse, code='A', name='Zone A', location_type='ZONE')
        self.shelf1 = StorageLocation.objects.create(
            warehouse=self.warehouse, parent=self.zone, code='A-1', name='Shelf 1', location_type='SHELF')
        self.shelf2 = StorageLocation.objects.create(
            warehouse=self.warehouse, parent=self.zone, code='A-2', name='Shelf 2', location_type='SHELF')

    def receipt_in_unloading(self, lines=None, **receipt_overrides):
        """
        Build a receipt through the real service layer and start unloading.
        `lines`: list of dicts of InvoiceLine overrides (default: one 200 kg line of MAT-1).
        """
        from apps.receiving import services
        from apps.receiving.models import GoodsReceipt
        gate, receiving = self.users['GATE'], self.users['RECEIVING']
        data = {
            'supplier': self.supplier, 'invoice_number': f'INV-{GoodsReceipt.objects.count() + 1}',
            'invoice_date': timezone.localdate(), 'vehicle_number': 'OD02AB1234',
            'arrival_datetime': timezone.now(),
        }
        data.update(receipt_overrides)
        receipt = services.create_receipt(data, gate)
        for overrides in (lines or [{}]):
            line = {'material': self.material, 'declared_quantity': Decimal('200'), 'declared_unit': self.kg,
                    'batch_number': 'LOT-A'}
            line.update(overrides)
            services.add_invoice_line(receipt, line, gate)
        for name, user in (('register_at_gate', gate), ('submit_for_unloading', gate), ('start_unloading', receiving)):
            receipt = services.transition_receipt(receipt, name, user)
        return receipt

    def verify(self, line, received='200', unit=None, user=None, **extra):
        from apps.receiving import services
        data = {'received_quantity': Decimal(received), 'received_unit': unit or self.kg}
        data.update(extra)
        return services.record_verification(line, data, user or self.users['RECEIVING'])

    def packaged_receipt(self, count=2, each='100', material=None, **receipt_overrides):
        """Receipt unloaded with `count` labelled packages awaiting put-away."""
        from apps.inventory import services as inv
        from apps.receiving import services
        receipt = self.receipt_in_unloading(
            lines=[{'material': material or self.material,
                    'declared_quantity': Decimal(each) * count}], **receipt_overrides)
        line = receipt.invoice_lines.get()
        self.verify(line, received=str(Decimal(each) * count))
        packages = inv.create_packages(line, [{'quantity': Decimal(each)}] * count, self.users['RECEIVING'])
        for pkg in packages:
            inv.record_label_print(pkg, self.users['RECEIVING'])
        receipt = services.transition_receipt(receipt, 'mark_verification_pending', self.users['RECEIVING'])
        receipt = services.transition_receipt(receipt, 'mark_unloaded', self.users['RECEIVING'])
        return receipt, [type(p).objects.get(pk=p.pk) for p in packages]

    def receipt_payload(self, **overrides):
        data = {
            'supplier': self.supplier.pk,
            'invoice_number': 'INV-001',
            'invoice_date': '2026-10-01',
            'vehicle_number': 'OD02AB1234',
            'arrival_datetime': timezone.now().isoformat(),
            'gross_weight': '15000.0000',
            'tare_weight': '5000.0000',
            'weight_unit': self.kg.pk,
            'weighing_method': 'WEIGHBRIDGE',
        }
        data.update(overrides)
        return {k: v for k, v in data.items() if v is not None}

    def line_payload(self, receipt_id, **overrides):
        data = {
            'receipt': str(receipt_id),
            'material': self.material.pk,
            'declared_quantity': '200.0000',
            'declared_unit': self.kg.pk,
            'declared_weight': '10000.0000',
            'declared_weight_unit': self.kg.pk,
            'batch_number': 'LOT-A',
        }
        data.update(overrides)
        return data


D = Decimal
