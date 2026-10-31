"""
Phases 3–4 — packages, labels, scanning, put-away, transfers, repacking, ledger, stock.
Covers spec test items 3, 4, 6, 9 (availability), 12, 14 and 15.
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, SystemConfiguration
from apps.core.testing import Fixtures, client_for
from apps.inventory import services as inv
from apps.inventory.models import (
    Package, PackageStatus, QCStatus, InventoryMovement, PackageLabelEvent, MovementReason,
)
from apps.master.models import Material
from apps.receiving.models import GoodsReceipt, ReceiptStatus

PKG = '/api/v1/inventory/packages'
STOCK = '/api/v1/inventory/stock'


def fresh(pkg):
    return Package.objects.get(pk=pkg.pk)


class PackageTestBase(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        self.client_r = client_for(self.fx.users['RECEIVING'])
        self.receipt = self.fx.receipt_in_unloading()
        self.line = self.receipt.invoice_lines.get()

    def create(self, payload, key=None, client=None):
        headers = {'HTTP_IDEMPOTENCY_KEY': key} if key else {}
        return (client or self.client_r).post(
            f'/api/v1/receiving/invoice-lines/{self.line.id}/packages/', payload, format='json', **headers)


class PackageCreationTests(PackageTestBase):
    def test_count_times_quantity_creates_unique_traceable_packages(self):
        self.fx.verify(self.line)
        resp = self.create({'count': 4, 'quantity_each': '50'})
        self.assertEqual(resp.status_code, 201, resp.content)
        ids = [p['package_id'] for p in resp.json()]
        self.assertEqual(len(set(ids)), 4)
        for pkg in Package.objects.all():
            self.assertEqual(pkg.invoice_line, self.line)
            self.assertEqual(pkg.status, PackageStatus.CREATED)
            self.assertEqual(pkg.qc_status, QCStatus.PENDING_SAMPLING)
            self.assertEqual(pkg.batch_number, 'LOT-A')
            self.assertEqual(pkg.received_at, GoodsReceipt.objects.get().arrival_datetime)
            mv = pkg.movements.get()
            self.assertEqual((mv.reason, mv.quantity_delta, mv.balance_after), (MovementReason.RECEIPT, Decimal('50'), Decimal('50')))
        self.assertEqual(AuditEvent.objects.filter(entity_type='Package', action='CREATE').count(), 4)

    def test_explicit_packages_with_lot_overrides(self):
        self.fx.verify(self.line)
        resp = self.create({'packages': [{'quantity': '120', 'batch_number': 'LOT-X'}, {'quantity': '80'}]})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(sorted(Package.objects.values_list('batch_number', flat=True)), ['LOT-A', 'LOT-X'])

    def test_cannot_exceed_accepted_quantity_cumulatively(self):
        self.fx.verify(self.line, received='200', damaged_quantity=Decimal('20'))  # accepted 180
        # the damage discrepancy must be accepted before packaging
        resp = self.create({'count': 1, 'quantity_each': '10'})
        self.assertEqual(resp.json()['error'], 'line_not_verified')
        from apps.receiving import services
        services.accept_discrepancy(self.line.verification, self.fx.users['MANAGER'], reason='damage noted')
        self.assertEqual(self.create({'count': 1, 'quantity_each': '100'}).status_code, 201)
        resp = self.create({'count': 1, 'quantity_each': '81'})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['details']['remaining'], '80.0000')
        self.assertEqual(self.create({'count': 1, 'quantity_each': '80'}).status_code, 201)

    def test_unverified_line_cannot_be_packaged(self):
        self.assertEqual(self.create({'count': 1, 'quantity_each': '10'}).json()['error'], 'line_not_verified')

    def test_request_shape_validation(self):
        self.fx.verify(self.line)
        self.assertEqual(self.create({'count': 2}).status_code, 400)
        self.assertEqual(self.create({'count': 1, 'quantity_each': '0'}).status_code, 400)
        self.assertEqual(self.create({'packages': [{'quantity': '1'}], 'count': 1, 'quantity_each': '1'}).status_code, 400)

    def test_expiry_required_for_expiry_controlled_material(self):
        self.fx.material.requires_expiry = True
        self.fx.material.save()
        self.fx.verify(self.line)
        self.assertEqual(self.create({'count': 1, 'quantity_each': '10'}).status_code, 400)
        expiry = (timezone.localdate() + timedelta(days=200)).isoformat()
        self.assertEqual(self.create({'count': 1, 'quantity_each': '10', 'expiry_date': expiry}).status_code, 201)

    def test_qc_not_required_material(self):
        receipt = self.fx.receipt_in_unloading(lines=[{'material': self.fx.material_noqc}])
        line = receipt.invoice_lines.get()
        self.fx.verify(line)
        pkg = inv.create_packages(line, [{'quantity': Decimal('200')}], self.fx.users['RECEIVING'])[0]
        self.assertEqual(pkg.qc_status, QCStatus.NOT_REQUIRED)

    def test_no_new_packages_after_unloaded(self):
        receipt, _ = self.fx.packaged_receipt()
        line = receipt.invoice_lines.get()
        resp = self.client_r.post(f'/api/v1/receiving/invoice-lines/{line.id}/packages/',
                                  {'count': 1, 'quantity_each': '1'}, format='json')
        self.assertEqual(resp.json()['error'], 'receipt_not_unloading')

    def test_role(self):
        self.fx.verify(self.line)
        for role in ('GATE', 'LAB_TECH', 'FACTORY', 'AUDITOR'):
            resp = self.create({'count': 1, 'quantity_each': '1'}, client=client_for(self.fx.users[role]))
            self.assertEqual(resp.status_code, 403, role)

    def test_packages_cannot_be_created_or_edited_directly(self):
        admin = client_for(self.fx.users['ADMIN'])
        self.assertEqual(admin.post(f'{PKG}/', {}, format='json').status_code, 405)
        self.fx.verify(self.line)
        pkg = inv.create_packages(self.line, [{'quantity': Decimal('10')}], self.fx.users['RECEIVING'])[0]
        self.assertEqual(admin.patch(f'{PKG}/{pkg.id}/', {'current_quantity': '999'}, format='json').status_code, 405)


class IdempotencyTests(PackageTestBase):
    """Spec test item 4 — retry-safe package creation and unique ID enforcement."""

    def setUp(self):
        super().setUp()
        self.fx.verify(self.line)

    def test_retry_with_same_key_returns_original_packages(self):
        first = self.create({'count': 2, 'quantity_each': '50'}, key='abc-1')
        second = self.create({'count': 2, 'quantity_each': '50'}, key='abc-1')
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second['Idempotent-Replayed'], 'true')
        self.assertEqual([p['package_id'] for p in first.json()], [p['package_id'] for p in second.json()])
        self.assertEqual(Package.objects.count(), 2)

    def test_same_key_different_payload_rejected(self):
        self.create({'count': 2, 'quantity_each': '50'}, key='abc-2')
        resp = self.create({'count': 3, 'quantity_each': '50'}, key='abc-2')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(Package.objects.count(), 2)

    def test_failed_request_does_not_burn_the_key(self):
        self.assertEqual(self.create({'count': 1, 'quantity_each': '999'}, key='abc-3').status_code, 400)
        self.assertEqual(self.create({'count': 1, 'quantity_each': '50'}, key='abc-3').status_code, 201)

    def test_without_key_each_request_creates(self):
        self.create({'count': 1, 'quantity_each': '50'})
        self.create({'count': 1, 'quantity_each': '50'})
        self.assertEqual(Package.objects.count(), 2)

    def test_package_ids_are_unique_in_database(self):
        from django.db import IntegrityError, transaction
        pkg = inv.create_packages(self.line, [{'quantity': Decimal('10')}], self.fx.users['RECEIVING'])[0]
        dup = Package(package_id=pkg.package_id, invoice_line=self.line, material=self.fx.material,
                      supplier=self.fx.supplier, original_quantity=1, current_quantity=1, unit=self.fx.kg,
                      received_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            dup.save()


class LabelAndScanTests(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        receipt = self.fx.receipt_in_unloading()
        self.line = receipt.invoice_lines.get()
        self.fx.verify(self.line)
        self.pkg = inv.create_packages(self.line, [{'quantity': Decimal('200')}], self.fx.users['RECEIVING'])[0]
        self.client_r = client_for(self.fx.users['RECEIVING'])

    def print_label(self, **data):
        return self.client_r.post(f'{PKG}/{self.pkg.id}/print-label/', data, format='json')

    def test_first_print_then_reprint_requires_reason(self):
        resp = self.print_label()
        self.assertEqual(resp.json()['status'], 'AWAITING_PUTAWAY')
        self.assertEqual(self.print_label().status_code, 400)
        resp = self.print_label(reason='Label torn')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['package_id'], self.pkg.package_id)  # reprint never changes the ID
        self.assertEqual(resp.json()['label_print_count'], 2)
        events = list(PackageLabelEvent.objects.filter(package=self.pkg).values_list('event_type', 'reprint_reason'))
        self.assertEqual(events, [('PRINT', ''), ('REPRINT', 'Label torn')])
        self.assertEqual(AuditEvent.objects.filter(entity_id=str(self.pkg.id), action='PRINT').count(), 2)

    def test_label_data_has_no_mutable_status(self):
        data = self.client_r.get(f'{PKG}/{self.pkg.id}/label/').json()
        self.assertEqual(data['package_id'], self.pkg.package_id)
        self.assertTrue(data['qr_payload'].endswith(f'/scan/{self.pkg.scan_token}'))
        self.assertNotIn(self.pkg.package_id, data['qr_payload'])
        for key in data:
            self.assertNotIn('status', key)

    def test_qr_png(self):
        resp = self.client_r.get(f'{PKG}/{self.pkg.id}/qr/')
        self.assertEqual(resp['Content-Type'], 'image/png')
        self.assertTrue(resp.content.startswith(b'\x89PNG'))

    def test_resolve_by_qr_url_token_and_printed_id(self):
        for code in (inv.qr_payload(self.pkg), self.pkg.scan_token, self.pkg.package_id.lower()):
            resp = self.client_r.get(f'{PKG}/resolve/', {'code': code})
            self.assertEqual(resp.status_code, 200, code)
            self.assertEqual(resp.json()['package_id'], self.pkg.package_id)
        self.assertEqual(AuditEvent.objects.filter(entity_id=str(self.pkg.id), action='SCAN').count(), 3)
        self.assertEqual(self.client_r.get(f'{PKG}/resolve/', {'code': 'nope'}).status_code, 404)

    def test_scan_reflects_current_server_state(self):
        before = self.client_r.get(f'{PKG}/resolve/', {'code': self.pkg.scan_token}).json()
        self.print_label()
        after = self.client_r.get(f'{PKG}/resolve/', {'code': self.pkg.scan_token}).json()
        self.assertEqual((before['status'], after['status']), ('CREATED', 'AWAITING_PUTAWAY'))

    def test_label_roles(self):
        resp = client_for(self.fx.users['FACTORY']).post(f'{PKG}/{self.pkg.id}/print-label/', {}, format='json')
        self.assertEqual(resp.status_code, 403)

    def test_label_event_is_immutable(self):
        self.print_label()
        event = PackageLabelEvent.objects.get()
        event.reprint_reason = 'edited'
        with self.assertRaises(ValueError):
            event.save()


class PutawayTests(TestCase):
    """Spec test item 6 — location movement and history."""

    def setUp(self):
        self.fx = Fixtures()
        self.receipt, self.pkgs = self.fx.packaged_receipt(count=2)
        self.client_w = client_for(self.fx.users['WAREHOUSE'])

    def putaway(self, pkg, location, client=None):
        return (client or self.client_w).post(f'{PKG}/{pkg.id}/putaway/', {'location': location.pk}, format='json')

    def test_pending_qc_must_go_to_quarantine(self):
        resp = self.putaway(self.pkgs[0], self.fx.shelf1)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'quarantine_required')
        resp = self.putaway(self.pkgs[0], self.fx.quarantine)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual((body['status'], body['current_location']), ('STORED', self.fx.quarantine.pk))
        self.assertFalse(body['is_available'])  # stored ≠ approved for use

    def test_no_qc_material_can_go_to_regular_shelf(self):
        _, pkgs = self.fx.packaged_receipt(count=1, material=self.fx.material_noqc, invoice_number='NOQC')
        self.assertEqual(self.putaway(pkgs[0], self.fx.shelf1).status_code, 200)
        self.assertTrue(fresh(pkgs[0]).is_available)

    def test_non_leaf_and_inactive_locations_rejected(self):
        self.assertEqual(self.putaway(self.pkgs[0], self.fx.zone).json()['error'], 'location_not_storable')
        self.fx.quarantine.is_active = False
        self.fx.quarantine.save()
        self.assertEqual(self.putaway(self.pkgs[0], self.fx.quarantine).json()['error'], 'location_inactive')

    def test_label_must_be_printed_first(self):
        receipt = self.fx.receipt_in_unloading(invoice_number='NOLABEL')
        line = receipt.invoice_lines.get()
        self.fx.verify(line)
        pkg = inv.create_packages(line, [{'quantity': Decimal('200')}], self.fx.users['RECEIVING'])[0]
        self.assertEqual(self.putaway(pkg, self.fx.quarantine).json()['error'], 'label_not_printed')

    def test_receipt_must_be_unloaded_first(self):
        receipt = self.fx.receipt_in_unloading(invoice_number='EARLY')
        line = receipt.invoice_lines.get()
        self.fx.verify(line)
        pkg = inv.create_packages(line, [{'quantity': Decimal('200')}], self.fx.users['RECEIVING'])[0]
        inv.record_label_print(pkg, self.fx.users['RECEIVING'])
        self.assertEqual(self.putaway(pkg, self.fx.quarantine).json()['error'], 'receipt_not_unloaded')

    def test_receipt_advances_to_stored_when_all_put_away(self):
        self.putaway(self.pkgs[0], self.fx.quarantine)
        self.assertEqual(GoodsReceipt.objects.get().status, ReceiptStatus.PUTAWAY_IN_PROGRESS)
        self.putaway(self.pkgs[1], self.fx.quarantine)
        receipt = GoodsReceipt.objects.get()
        self.assertEqual(receipt.status, ReceiptStatus.STORED)
        self.assertIsNotNone(receipt.stored_at)

    def test_cannot_put_away_twice(self):
        self.putaway(self.pkgs[0], self.fx.quarantine)
        self.assertEqual(self.putaway(self.pkgs[0], self.fx.quarantine).status_code, 400)

    def test_role(self):
        self.assertEqual(self.putaway(self.pkgs[0], self.fx.quarantine, client=client_for(self.fx.users['GATE'])).status_code, 403)

    def test_transfer_and_chronological_history(self):
        pkg = self.pkgs[0]
        self.putaway(pkg, self.fx.quarantine)
        quarantine2 = self.fx.quarantine.__class__.objects.create(
            warehouse=self.fx.warehouse, code='Q2', name='Quarantine 2', location_type='QUARANTINE', is_quarantine=True)
        self.assertEqual(self.client_w.post(f'{PKG}/{pkg.id}/transfer/', {'location': self.fx.shelf1.pk}, format='json').json()['error'],
                         'quarantine_required')
        self.assertEqual(self.client_w.post(f'{PKG}/{pkg.id}/transfer/', {'location': self.fx.quarantine.pk}, format='json').status_code, 400)
        self.assertEqual(self.client_w.post(f'{PKG}/{pkg.id}/transfer/', {'location': quarantine2.pk}, format='json').status_code, 200)
        Package.objects.filter(pk=pkg.pk).update(qc_status=QCStatus.PASSED)  # stand-in for lab release (Phase 5)
        resp = self.client_w.post(f'{PKG}/{pkg.id}/transfer/', {'location': self.fx.shelf1.pk, 'notes': 'released'}, format='json')
        self.assertEqual(resp.status_code, 200)
        history = self.client_w.get(f'{PKG}/{pkg.id}/movements/').json()
        self.assertEqual([m['reason'] for m in history], ['RECEIPT', 'PUTAWAY', 'TRANSFER', 'QC_MOVE'])
        self.assertEqual([m['to_location'] for m in history[1:]], [self.fx.quarantine.pk, quarantine2.pk, self.fx.shelf1.pk])
        self.assertEqual(history[2]['from_location'], self.fx.quarantine.pk)
        self.assertEqual(fresh(pkg).current_location, self.fx.shelf1)  # exactly one current location

    def test_transfer_role(self):
        self.putaway(self.pkgs[0], self.fx.quarantine)
        resp = client_for(self.fx.users['RECEIVING']).post(
            f'{PKG}/{self.pkgs[0].id}/transfer/', {'location': self.fx.quarantine.pk}, format='json')
        self.assertEqual(resp.status_code, 403)


class SplitTests(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        _, pkgs = self.fx.packaged_receipt(count=1, each='100')
        self.pkg = pkgs[0]
        inv.putaway_package(self.pkg, self.fx.quarantine, self.fx.users['WAREHOUSE'])
        self.client_w = client_for(self.fx.users['WAREHOUSE'])

    def split(self, quantities, reason='Repack into 25 kg bags'):
        return self.client_w.post(f'{PKG}/{self.pkg.id}/split/', {'quantities': quantities, 'reason': reason}, format='json')

    def test_partial_split_preserves_lineage_and_ledger(self):
        resp = self.split(['30', '20'])
        self.assertEqual(resp.status_code, 201, resp.content)
        parent = fresh(self.pkg)
        self.assertEqual(parent.current_quantity, Decimal('50'))
        self.assertEqual(parent.original_quantity, Decimal('100'))
        self.assertEqual(parent.status, PackageStatus.STORED)
        children = Package.objects.filter(parent_package=parent)
        self.assertEqual(sorted(c.current_quantity for c in children), [Decimal('20'), Decimal('30')])
        for c in children:
            self.assertEqual((c.status, c.qc_status, c.batch_number, c.received_at, c.current_location),
                             (PackageStatus.CREATED, parent.qc_status, parent.batch_number, parent.received_at,
                              parent.current_location))
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_full_split_marks_parent_repacked(self):
        self.split(['50', '50'])
        self.assertEqual(fresh(self.pkg).status, PackageStatus.REPACKED)
        self.assertEqual(fresh(self.pkg).current_quantity, 0)

    def test_split_validation(self):
        self.assertEqual(self.split(['101']).json()['error'], 'insufficient_quantity')
        self.assertEqual(self.split(['10'], reason='').status_code, 400)
        self.assertEqual(self.split([]).status_code, 400)

    def test_child_needs_label_then_putaway(self):
        child_id = self.split(['40']).json()[0]['id']
        child = Package.objects.get(pk=child_id)
        self.assertEqual(self.client_w.post(f'{PKG}/{child.id}/putaway/', {'location': self.fx.quarantine.pk}, format='json').json()['error'],
                         'label_not_printed')
        inv.record_label_print(child, self.fx.users['WAREHOUSE'])
        self.assertEqual(self.client_w.post(f'{PKG}/{child.id}/putaway/', {'location': self.fx.quarantine.pk}, format='json').status_code, 200)

    def test_child_does_not_count_as_new_receipt_stock(self):
        self.split(['40'])
        self.assertEqual(inv.packaged_quantity(self.pkg.invoice_line), Decimal('100'))


class AvailabilityTests(TestCase):
    """Spec test item 9 (eligibility part) — blocked stock is never available."""

    def setUp(self):
        self.fx = Fixtures()
        _, pkgs = self.fx.packaged_receipt(count=1)
        inv.putaway_package(pkgs[0], self.fx.quarantine, self.fx.users['WAREHOUSE'])
        self.pkg = fresh(pkgs[0])

    def set(self, **fields):
        Package.objects.filter(pk=self.pkg.pk).update(**fields)
        return fresh(self.pkg)

    def assert_available(self, pkg, expected):
        self.assertEqual(pkg.is_available, expected, pkg.blocking_reasons())
        self.assertEqual(Package.objects.available().filter(pk=pkg.pk).exists(), expected)

    def test_pending_qc_is_not_available(self):
        self.assert_available(self.pkg, False)
        self.assertIn('QC status is Pending Sampling', self.pkg.blocking_reasons())

    def test_released_is_available(self):
        self.assert_available(self.set(qc_status=QCStatus.PASSED), True)

    def test_each_blocking_condition(self):
        today = timezone.localdate()
        for fields in ({'qc_status': QCStatus.FAILED}, {'qc_status': QCStatus.ON_HOLD},
                       {'qc_status': QCStatus.RETEST_REQUIRED}, {'is_on_hold': True}, {'is_recalled': True},
                       {'expiry_date': today}, {'retest_date': today - timedelta(days=1)},
                       {'current_quantity': 0}):
            pkg = self.set(qc_status=QCStatus.PASSED, is_on_hold=False, is_recalled=False,
                           expiry_date=None, retest_date=None, current_quantity=Decimal('100'))
            self.assert_available(pkg, True)
            self.assert_available(self.set(**fields), False)

    def test_not_stored_is_not_available(self):
        _, pkgs = self.fx.packaged_receipt(count=1, material=self.fx.material_noqc, invoice_number='N2')
        self.assert_available(pkgs[0], False)  # awaiting put-away

    def test_api_filter(self):
        self.set(qc_status=QCStatus.PASSED)
        r = client_for(self.fx.users['AUDITOR']).get(f'{PKG}/', {'available': 'true'}).json()
        self.assertEqual([p['package_id'] for p in r['results']], [self.pkg.package_id])


class StockQueryTests(TestCase):
    """Spec test item 15 — dashboard/report totals reconcile with inventory records."""

    def setUp(self):
        self.fx = Fixtures()
        self.auditor = client_for(self.fx.users['AUDITOR'])
        _, a = self.fx.packaged_receipt(count=2, each='100')
        _, b = self.fx.packaged_receipt(count=1, each='50', material=self.fx.material_noqc, invoice_number='B')
        for p in a:
            inv.putaway_package(p, self.fx.quarantine, self.fx.users['WAREHOUSE'])
        inv.putaway_package(b[0], self.fx.shelf1, self.fx.users['WAREHOUSE'])
        self.a, self.b = [fresh(p) for p in a], fresh(b[0])

    def test_balances_by_material_and_qc_status(self):
        rows = self.auditor.get(f'{STOCK}/balances/', {'group_by': 'material,qc_status'}).json()['results']
        by_code = {(r['material__code'], r['qc_status']): r for r in rows}
        self.assertEqual(Decimal(by_code[('MAT-1', 'PENDING_SAMPLING')]['quantity']), Decimal('200'))
        self.assertEqual(Decimal(by_code[('MAT-1', 'PENDING_SAMPLING')]['available_quantity']), 0)
        self.assertEqual(Decimal(by_code[('MAT-2', 'NOT_REQUIRED')]['available_quantity']), Decimal('50'))
        total = sum(Decimal(r['quantity']) for r in rows)
        self.assertEqual(total, sum(p.current_quantity for p in Package.objects.in_warehouse()))

    def test_balances_never_mix_units(self):
        receipt = self.fx.receipt_in_unloading(lines=[{'declared_quantity': Decimal('10'), 'declared_unit': self.fx.pcs}],
                                               invoice_number='PCS')
        line = receipt.invoice_lines.get()
        self.fx.verify(line, received='10', unit=self.fx.pcs)
        inv.create_packages(line, [{'quantity': Decimal('10')}], self.fx.users['RECEIVING'])
        rows = self.auditor.get(f'{STOCK}/balances/', {'group_by': 'material'}).json()['results']
        mat1 = sorted((r['unit__code'], r['quantity']) for r in rows if r['material__code'] == 'MAT-1')
        self.assertEqual(mat1, [('KG', '200.0000'), ('PCS', '10.0000')])

    def test_invalid_group_by(self):
        self.assertEqual(self.auditor.get(f'{STOCK}/balances/', {'group_by': 'colour'}).status_code, 400)

    def test_ageing_buckets_configurable(self):
        Package.objects.filter(pk=self.a[0].pk).update(received_at=timezone.now() - timedelta(days=45))
        data = self.auditor.get(f'{STOCK}/ageing/').json()
        self.assertEqual(data['buckets'], ['0-30', '31-60', '61-90', '91+'])
        mat1 = next(r for r in data['rows'] if r['material_code'] == 'MAT-1')
        self.assertEqual((mat1['quantities']['0-30'], mat1['quantities']['31-60']), ('100.0000', '100.0000'))
        SystemConfiguration.objects.update_or_create(key='stock.ageing_buckets', defaults={'value': '[7, 14]'})
        self.assertEqual(self.auditor.get(f'{STOCK}/ageing/').json()['buckets'], ['0-7', '8-14', '15+'])

    def test_expiry_exposure(self):
        today = timezone.localdate()
        Package.objects.filter(pk=self.a[0].pk).update(expiry_date=today + timedelta(days=10))
        Package.objects.filter(pk=self.a[1].pk).update(expiry_date=today + timedelta(days=100))
        Package.objects.filter(pk=self.b.pk).update(retest_date=today - timedelta(days=1))
        ids = [p['package_id'] for p in self.auditor.get(f'{STOCK}/expiry/').json()]
        self.assertEqual(sorted(ids), sorted([self.a[0].package_id, self.b.package_id]))

    def test_low_stock(self):
        Material.objects.filter(pk=self.fx.material_noqc.pk).update(min_stock_quantity=Decimal('60'), min_stock_unit=self.fx.kg)
        low = self.auditor.get(f'{STOCK}/low-stock/').json()
        self.assertEqual([(r['material_code'], r['available']) for r in low], [('MAT-2', '50.0000')])

    def test_dashboard_reconciles_with_records(self):
        d = self.auditor.get(f'{STOCK}/dashboard/').json()
        in_wh = Package.objects.in_warehouse()
        self.assertEqual(d['packages_in_warehouse'], in_wh.count())
        self.assertEqual(sum(d['by_status'].values()), in_wh.count())
        self.assertEqual(sum(d['by_qc_status'].values()), in_wh.count())
        self.assertEqual(d['available_packages'], Package.objects.available().count())
        self.assertEqual((d['pending_qc'], d['in_quarantine_location'], d['available_packages']), (2, 2, 1))


class LedgerAndReconciliationTests(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        _, pkgs = self.fx.packaged_receipt(count=2, each='100', material=self.fx.material_noqc)
        for p in pkgs:
            inv.putaway_package(p, self.fx.shelf1, self.fx.users['WAREHOUSE'])
        self.pkgs = [fresh(p) for p in pkgs]

    def test_clean_ledger_reconciles(self):
        resp = client_for(self.fx.users['AUDITOR']).get(f'{STOCK}/reconciliation/').json()
        self.assertEqual(resp, {'reconciled': True, 'issues': []})

    def test_tampered_balance_and_location_are_detected(self):
        Package.objects.filter(pk=self.pkgs[0].pk).update(current_quantity=Decimal('99'))
        Package.objects.filter(pk=self.pkgs[1].pk).update(current_location=self.fx.shelf2)
        issues = {i['package_id']: i['problems'] for i in inv.reconciliation_issues()}
        self.assertIn('ledger 100.0000', issues[self.pkgs[0].package_id][0])
        self.assertEqual(issues[self.pkgs[1].package_id], ['location differs from last ledger movement'])

    def test_movements_are_immutable(self):
        mv = InventoryMovement.objects.first()
        mv.notes = 'edited'
        with self.assertRaises(ValueError):
            mv.save()

    def test_quantity_change_cannot_go_negative(self):
        from django.db import transaction
        from apps.core.exceptions import BusinessRuleViolation
        with self.assertRaises(BusinessRuleViolation), transaction.atomic():
            pkg = Package.objects.select_for_update().get(pk=self.pkgs[0].pk)
            inv.apply_quantity_change(pkg, Decimal('-101'), reason=MovementReason.ADJUSTMENT, user=self.fx.users['ADMIN'])

    def test_management_command(self):
        from io import StringIO
        from django.core.management import call_command
        out = StringIO()
        call_command('reconcile_inventory', stdout=out)
        self.assertIn('reconciles', out.getvalue())
        Package.objects.filter(pk=self.pkgs[0].pk).update(current_quantity=Decimal('1'))
        with self.assertRaises(SystemExit):
            call_command('reconcile_inventory', stdout=StringIO(), stderr=StringIO())


class PackageTimelineTests(TestCase):
    def test_timeline_from_gate_to_current_state(self):
        fx = Fixtures()
        receipt, pkgs = fx.packaged_receipt(count=1)
        pkg = pkgs[0]
        inv.resolve_scan(pkg.package_id, fx.users['WAREHOUSE'])
        inv.putaway_package(pkg, fx.quarantine, fx.users['WAREHOUSE'])
        events = client_for(fx.users['AUDITOR']).get(f'{PKG}/{pkg.id}/timeline/').json()
        kinds = [e['kind'] for e in events]
        self.assertEqual(kinds[0], 'RECEIPT')
        for kind in ('VERIFICATION', 'CREATE', 'PRINT', 'SCAN', 'MOVEMENT', 'TRANSITION'):
            self.assertIn(kind, kinds)
        timestamps = [e['timestamp'] for e in events]
        self.assertEqual(timestamps, sorted(timestamps))
