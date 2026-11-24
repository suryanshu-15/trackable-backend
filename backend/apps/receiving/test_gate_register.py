"""
Gate register entry in one step, weighbridge at receiving (loaded truck before unloading, empty truck
after) and gate out.
"""
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent
from apps.core.testing import Fixtures, client_for, set_config
from apps.receiving.models import GoodsReceipt, ReceiptStatus

URL = '/api/v1/receiving/goods-receipts'


class GateRegisterTests(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        set_config(receiving__weigh_loaded_before_unloading=True, receiving__weigh_empty_before_finishing=True)
        self.gate = client_for(self.fx.users['GATE'])
        self.receiving = client_for(self.fx.users['RECEIVING'])

    def entry(self, send=True, **overrides):
        body = {
            'supplier': self.fx.supplier.pk, 'invoice_number': 'INV-77', 'invoice_date': '2026-11-20',
            'arrival_datetime': timezone.now().isoformat(), 'vehicle_number': 'od 02  ab 1234',
            'transporter_name': 'Kalinga Roadlines', 'lr_number': 'LR-5521', 'eway_bill_number': '321009876543',
            'driver_mobile': '9876543210', 'driver_licence_number': 'od02 20150012345',
            'lines': [
                {'material': self.fx.material.pk, 'declared_quantity': '1', 'declared_unit': self.fx.tonne.pk,
                 'storage_location': self.fx.shelf1.pk},
                {'material': self.fx.material_noqc.pk, 'declared_quantity': '250', 'declared_unit': self.fx.kg.pk},
            ],
            'send': send,
        }
        body.update(overrides)
        return self.gate.post(f'{URL}/gate-entry/', body, format='json')

    def weigh(self, rid, stage, weight, unit=None, client=None):
        return (client or self.receiving).post(f'{URL}/{rid}/weigh/', {
            'stage': stage, 'weight': weight, 'unit': (unit or self.fx.kg).pk}, format='json')

    def go(self, rid, action, client=None):
        return (client or self.receiving).post(f'{URL}/{rid}/{action}/', {}, format='json')

    def test_one_step_entry_registers_and_sends_to_receiving(self):
        resp = self.entry()
        self.assertEqual(resp.status_code, 201, resp.content)
        r = resp.json()
        self.assertEqual(r['status'], 'AWAITING_UNLOADING')
        self.assertRegex(r['gate_entry_number'], r'^GE-\d{4}-00001$')
        self.assertEqual(r['vehicle_number'], 'OD02AB1234')
        self.assertEqual(r['driver_licence_number'], 'OD0220150012345')
        self.assertEqual(self.gate.get(f'{URL}/', {'search': '20150012345'}).json()['count'], 1)
        self.assertEqual((r['transporter_name'], r['lr_number'], r['eway_bill_number']),
                         ('Kalinga Roadlines', 'LR-5521', '321009876543'))
        self.assertEqual([line['storage_location_code'] for line in r['invoice_lines']], ['A-1', None])
        self.assertEqual(r['invoice_lines'][0]['material_name_declared'], 'Test Material')
        second = self.entry(invoice_number='INV-78').json()
        self.assertTrue(second['gate_entry_number'].endswith('00002'))

    def test_draft_can_be_edited_then_sent(self):
        r = self.entry(send=False).json()
        self.assertEqual(r['status'], 'DRAFT')
        keep = r['invoice_lines'][0]
        body = {'supplier': self.fx.supplier.pk, 'invoice_number': 'INV-77', 'invoice_date': '2026-11-20',
                'arrival_datetime': r['arrival_datetime'], 'vehicle_number': 'OD02AB1234', 'send': True,
                'lines': [{'id': keep['id'], 'material': self.fx.material.pk, 'declared_quantity': '2',
                           'declared_unit': self.fx.tonne.pk}]}
        resp = self.gate.put(f"{URL}/{r['id']}/gate-entry/", body, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        r = resp.json()
        self.assertEqual((r['status'], len(r['invoice_lines'])), ('AWAITING_UNLOADING', 1))
        self.assertEqual(r['invoice_lines'][0]['declared_quantity'], '2.0000')

    def test_entry_needs_materials_and_gate_role(self):
        self.assertEqual(self.entry(lines=[]).status_code, 400)
        self.assertEqual(self.receiving.post(f'{URL}/gate-entry/', {}, format='json').status_code, 403)

    def test_weighbridge_flow_and_weight_check(self):
        rid = self.entry().json()['id']
        # Unloading cannot start before the loaded truck is weighed; the gate cannot weigh.
        self.assertEqual(self.go(rid, 'start-unloading').json()['error'], 'loaded_weight_missing')
        self.assertEqual(self.weigh(rid, 'LOADED', '9000', client=self.gate).status_code, 403)
        self.assertEqual(self.weigh(rid, 'EMPTY', '7000').json()['error'], 'loaded_weight_missing')
        r = self.weigh(rid, 'LOADED', '9260').json()
        self.assertEqual((r['gross_weight'], r['weighing_method']), ('9260.0000', 'WEIGHBRIDGE'))
        self.assertIsNotNone(r['gross_weighed_at'])
        self.assertEqual(self.weigh(rid, 'LOADED', '9300').json()['error'], 'already_weighed')
        self.assertEqual(self.go(rid, 'start-unloading').status_code, 200)

        receipt = GoodsReceipt.objects.get(pk=rid)
        line1, line2 = receipt.invoice_lines.order_by('line_number')
        for line, qty in ((line1, '1'), (line2, '0.25')):
            v = self.fx.verify(line, received=qty, unit=self.fx.tonne)
            self.assertEqual(v.verification_status, 'VERIFIED')
        from apps.inventory import services as inv
        for line, qty in ((line1, '1'), (line2, '0.25')):
            for p in inv.create_packages(line, [{'quantity': Decimal(qty)}], self.fx.users['RECEIVING']):
                inv.record_label_print(p, self.fx.users['RECEIVING'])
        self.assertEqual(self.go(rid, 'verification-pending').status_code, 200)
        # Finishing needs the empty weighing first.
        self.assertEqual(self.go(rid, 'mark-unloaded').json()['error'], 'empty_weight_missing')
        self.assertEqual(self.weigh(rid, 'EMPTY', '9.5', unit=self.fx.tonne).status_code, 400)  # heavier than loaded
        # Empty truck 8 t: net 1260 kg vs invoice 1 t + 250 kg = 1250 kg → 0.8 % > 0.5 % tolerance.
        r = self.weigh(rid, 'EMPTY', '8', unit=self.fx.tonne).json()
        self.assertEqual((r['tare_weight'], r['net_weight']), ('8000.0000', '1260.0000'))
        wd = r['weight_discrepancy']
        self.assertEqual((wd['declared_line_weight'], wd['difference'], wd['exceeds_tolerance']), ('1250', '10', True))
        self.assertTrue(AuditEvent.objects.filter(entity_id=rid, extra__weighing='EMPTY').exists())
        self.assertEqual(self.go(rid, 'mark-unloaded').json()['status'], 'UNLOADED')

    def test_gate_out(self):
        rid = self.entry().json()['id']
        url = f'{URL}/{rid}/gate-out/'
        self.assertEqual(self.gate.post(url, {}, format='json').json()['error'], 'truck_not_empty')
        self.weigh(rid, 'LOADED', '9260')
        self.go(rid, 'start-unloading')
        self.weigh(rid, 'EMPTY', '8000')
        self.assertEqual(self.receiving.post(url, {}, format='json').status_code, 403)
        r = self.gate.post(url, {}, format='json').json()
        self.assertIsNotNone(r['gate_out_at'])
        self.assertEqual(self.gate.post(url, {}, format='json').json()['error'], 'already_out')
        dash = self.gate.get(f'{URL}/dashboard/').json()
        self.assertEqual((dash['trucks_on_site'], dash['ready_to_leave']), (0, 0))

    def test_cancelled_truck_can_leave(self):
        rid = self.entry(send=False).json()['id']
        self.gate.post(f'{URL}/{rid}/cancel/', {'reason': 'Wrong material'}, format='json')
        self.assertEqual(GoodsReceipt.objects.get(pk=rid).status, ReceiptStatus.CANCELLED)
        self.assertEqual(self.gate.post(f'{URL}/{rid}/gate-out/', {}, format='json').status_code, 200)

    def test_editing_keeps_the_row_order_of_the_form(self):
        r = self.entry(send=False).json()
        first, second = r['invoice_lines']
        body = {'supplier': self.fx.supplier.pk, 'invoice_number': 'INV-77', 'invoice_date': '2026-11-20',
                'arrival_datetime': r['arrival_datetime'], 'vehicle_number': 'OD02AB1234',
                'lines': [{'material': self.fx.material.pk, 'declared_quantity': '3', 'declared_unit': self.fx.tonne.pk},
                          {'id': second['id'], 'material': self.fx.material_noqc.pk, 'declared_quantity': '250', 'declared_unit': self.fx.kg.pk},
                          {'id': first['id'], 'material': self.fx.material.pk, 'declared_quantity': '1', 'declared_unit': self.fx.tonne.pk}]}
        lines = self.gate.put(f"{URL}/{r['id']}/gate-entry/", body, format='json').json()['invoice_lines']
        self.assertEqual([(line['line_number'], line['declared_quantity']) for line in lines],
                         [(1, '3.0000'), (2, '250.0000'), (3, '1.0000')])

    def test_loaded_truck_must_weigh_more_than_the_invoice(self):
        lines = [{'material': self.fx.material.pk, 'declared_quantity': q, 'declared_unit': self.fx.tonne.pk} for q in ('100', '80', '5')]
        r = self.entry(lines=lines).json()
        self.assertEqual(r['invoice_weight_kg'], '185000')
        resp = self.weigh(r['id'], 'LOADED', '20', unit=self.fx.tonne)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'loaded_below_invoice')
        self.assertIn('185 t', resp.json()['message'])
        self.assertEqual(self.weigh(r['id'], 'LOADED', '185000').json()['error'], 'loaded_below_invoice')  # equal is not enough
        self.assertEqual(self.weigh(r['id'], 'LOADED', '201.5', unit=self.fx.tonne).status_code, 200)
        # A correction cannot bring it below the invoice weight either.
        url = f"{URL}/{r['id']}/correct-weights/"
        manager = client_for(self.fx.users['MANAGER'])
        self.assertEqual(self.receiving.post(url, {'gross_weight': '205', 'reason': 'x'}, format='json').status_code, 403)
        bad = manager.post(url, {'gross_weight': '20', 'reason': 'Typo'}, format='json')
        self.assertEqual(bad.json()['error'], 'loaded_below_invoice')
        self.assertEqual(manager.post(url, {'gross_weight': '205', 'reason': 'Re-weighed'}, format='json').status_code, 200)
