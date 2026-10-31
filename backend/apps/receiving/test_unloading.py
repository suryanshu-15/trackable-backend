"""
Phase 3 — Unloading verification & discrepancy handling. Covers spec test item 5
(expected vs actual mismatch) and the completion conditions of Stage 3.
"""
from decimal import Decimal

from django.test import TestCase

from apps.core.models import AuditEvent, SystemConfiguration
from apps.core.testing import Fixtures, client_for
from apps.inventory import services as inv
from apps.receiving.models import GoodsReceipt, InvoiceLine, ReceiptStatus

URL = '/api/v1/receiving'


class UnloadingTestCase(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        self.receiving = client_for(self.fx.users['RECEIVING'])
        self.manager = client_for(self.fx.users['MANAGER'])
        self.receipt = self.fx.receipt_in_unloading()
        self.line = self.receipt.invoice_lines.get()

    def verify(self, line=None, client=None, **data):
        payload = {'received_quantity': '200', 'received_unit': self.fx.kg.pk}
        payload.update(data)
        return (client or self.receiving).post(
            f'{URL}/invoice-lines/{(line or self.line).id}/verify/', payload, format='json')

    def receipt_status(self):
        # django-fsm protected fields cannot be refresh_from_db()'d; re-fetch instead
        return GoodsReceipt.objects.get(pk=self.receipt.pk).status


class VerificationTests(UnloadingTestCase):
    def test_exact_match_is_verified(self):
        resp = self.verify()
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['verification_status'], 'VERIFIED')
        self.assertEqual(body['discrepancy_status'], 'NONE')
        self.assertEqual(Decimal(body['accepted_quantity']), Decimal('200'))
        self.assertEqual(Decimal(body['short_quantity']), 0)
        self.assertEqual(self.receipt_status(), ReceiptStatus.UNLOADING_IN_PROGRESS)

    def test_short_quantity_flags_receipt_without_touching_invoice(self):
        resp = self.verify(received_quantity='180')
        body = resp.json()
        self.assertEqual(body['verification_status'], 'DISCREPANT')
        self.assertEqual(body['discrepancy_status'], 'QUANTITY_SHORT')
        self.assertEqual(Decimal(body['short_quantity']), Decimal('20'))
        self.assertEqual(self.receipt_status(), ReceiptStatus.EXCEPTION)
        self.line.refresh_from_db()
        self.assertEqual(self.line.declared_quantity, Decimal('200'))  # declared value preserved
        auto = AuditEvent.objects.get(entity_id=str(self.receipt.id), extra__transition='flag_exception')
        self.assertIn('QUANTITY_SHORT', auto.reason)

    def test_excess_and_damage_is_multiple(self):
        body = self.verify(received_quantity='210', damaged_quantity='5').json()
        self.assertEqual(body['discrepancy_status'], 'MULTIPLE')
        self.assertEqual(Decimal(body['excess_quantity']), Decimal('10'))
        self.assertEqual(Decimal(body['accepted_quantity']), Decimal('205'))

    def test_client_cannot_supply_computed_fields(self):
        body = self.verify(received_quantity='180', short_quantity='0', verification_status='VERIFIED').json()
        self.assertEqual(Decimal(body['short_quantity']), Decimal('20'))
        self.assertEqual(body['verification_status'], 'DISCREPANT')

    def test_configured_unit_conversion_is_used(self):
        receipt = self.fx.receipt_in_unloading(lines=[{'declared_quantity': Decimal('2'), 'declared_unit': self.fx.tonne}])
        body = self.verify(line=receipt.invoice_lines.get(), received_quantity='2000').json()
        self.assertEqual(body['verification_status'], 'VERIFIED')

    def test_unit_without_conversion_is_flagged(self):
        body = self.verify(received_unit=self.fx.pcs.pk).json()
        self.assertEqual(body['discrepancy_status'], 'UNIT_MISMATCH')
        self.assertIsNone(body['short_quantity'])

    def test_observed_grade_difference_is_description_mismatch(self):
        body = self.verify(observed_grade='Grade B').json()
        self.assertEqual(body['discrepancy_status'], 'DESCRIPTION_MISMATCH')

    def test_quantity_validation(self):
        self.assertEqual(self.verify(received_quantity='10', damaged_quantity='11').status_code, 400)
        self.assertEqual(self.verify(received_quantity='100', damaged_quantity='10', accepted_quantity='95').status_code, 400)
        self.assertEqual(self.verify(received_quantity='-1').status_code, 400)

    def test_partial_acceptance_is_discrepant_only_if_figures_differ(self):
        body = self.verify(accepted_quantity='150').json()
        # 200 received, nothing damaged — accepting less is allowed and recorded as given
        self.assertEqual(Decimal(body['accepted_quantity']), Decimal('150'))

    def test_verify_only_while_unloading(self):
        receipt = self.fx.receipt_in_unloading()
        from apps.receiving import services
        # A receipt still awaiting unloading cannot be verified
        other = services.create_receipt({
            'supplier': self.fx.supplier2, 'invoice_number': 'X', 'invoice_date': '2026-01-01',
            'vehicle_number': 'V', 'arrival_datetime': '2026-01-01T10:00Z'}, self.fx.users['GATE'])
        line = services.add_invoice_line(other, {'material': self.fx.material, 'declared_quantity': Decimal('1'),
                                                 'declared_unit': self.fx.kg}, self.fx.users['GATE'])
        resp = self.verify(line=line)
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'receipt_not_unloading')
        self.assertEqual(self.verify(line=receipt.invoice_lines.get()).status_code, 200)

    def test_gate_operator_cannot_verify(self):
        self.assertEqual(self.verify(client=client_for(self.fx.users['GATE'])).status_code, 403)

    def test_re_recording_is_audited_with_before_and_after(self):
        self.verify(received_quantity='180')
        self.verify(received_quantity='200')
        events = AuditEvent.objects.filter(entity_type='ReceiptLineVerification').order_by('timestamp')
        self.assertEqual([e.action for e in events], ['CREATE', 'UPDATE'])
        self.assertEqual(events[1].before_state['received_quantity'], '180.0000')
        self.assertEqual(events[1].after_state['verification_status'], 'VERIFIED')

    def test_timeline_includes_verification(self):
        self.verify(received_quantity='180')
        timeline = client_for(self.fx.users['GATE']).get(f'{URL}/goods-receipts/{self.receipt.id}/timeline/').json()
        self.assertIn('ReceiptLineVerification', [e['entity_type'] for e in timeline])


class DiscrepancyReviewTests(UnloadingTestCase):
    def setUp(self):
        super().setUp()
        self.v_id = self.verify(received_quantity='180').json()['id']

    def accept(self, client, reason='Supplier credit note agreed'):
        return client.post(f'{URL}/verifications/{self.v_id}/accept-discrepancy/', {'reason': reason}, format='json')

    def test_manager_accepts_with_reason(self):
        self.assertEqual(self.accept(self.manager, reason='').status_code, 400)
        resp = self.accept(self.manager)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['verification_status'], 'EXCEPTION_ACCEPTED')
        self.assertEqual(resp.json()['exception_authorized_by'], self.fx.users['MANAGER'].pk)

    def test_worker_role_cannot_accept(self):
        self.assertEqual(self.accept(self.receiving).status_code, 403)

    def test_separation_of_duties(self):
        admin = client_for(self.fx.users['ADMIN'])
        v2 = self.verify(client=admin, received_quantity='170').json()['id']
        resp = admin.post(f'{URL}/verifications/{v2}/accept-discrepancy/', {'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(resp.json()['error'], 'separation_of_duties')

    def test_separation_can_be_disabled_by_config(self):
        SystemConfiguration.objects.update_or_create(key='receiving.discrepancy_acceptance_separation', defaults={'value': 'false'})
        admin = client_for(self.fx.users['ADMIN'])
        v2 = self.verify(client=admin, received_quantity='170').json()['id']
        self.assertEqual(admin.post(f'{URL}/verifications/{v2}/accept-discrepancy/', {'reason': 'x'}, format='json').status_code, 200)

    def test_re_recording_voids_acceptance(self):
        self.accept(self.manager)
        body = self.verify(received_quantity='175').json()
        self.assertEqual(body['verification_status'], 'DISCREPANT')
        self.assertIsNone(body['exception_authorized_by'])

    def test_verified_line_cannot_be_accepted(self):
        v = self.verify(received_quantity='200').json()['id']
        resp = self.manager.post(f'{URL}/verifications/{v}/accept-discrepancy/', {'reason': 'x'}, format='json')
        self.assertEqual(resp.status_code, 400)


class CompletionTests(UnloadingTestCase):
    def transition(self, name):
        return self.receiving.post(f'{URL}/goods-receipts/{self.receipt.id}/{name}/', {}, format='json')

    def test_verification_pending_requires_all_lines_resolved(self):
        resp = self.transition('verification-pending')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['details']['lines'], [{'line_number': 1, 'problem': 'not verified'}])

        self.verify(received_quantity='180')  # → EXCEPTION
        resp = self.transition('verification-pending')
        self.assertEqual(resp.json()['details']['lines'][0]['problem'], 'discrepancy awaiting review')

        v = self.line.verification
        self.manager.post(f'{URL}/verifications/{v.id}/accept-discrepancy/', {'reason': 'ok'}, format='json')
        self.assertEqual(self.transition('verification-pending').status_code, 200)

    def test_unloaded_requires_full_packaging(self):
        self.verify()
        self.transition('verification-pending')
        resp = self.transition('mark-unloaded')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('packaged 0', resp.json()['details']['lines'][0]['problem'])

        inv.create_packages(self.line, [{'quantity': Decimal('150')}], self.fx.users['RECEIVING'])
        self.assertEqual(self.transition('mark-unloaded').status_code, 400)
        inv.create_packages(self.line, [{'quantity': Decimal('50')}], self.fx.users['RECEIVING'])
        resp = self.transition('mark-unloaded')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'UNLOADED')

    def test_zero_accepted_line_needs_no_packages(self):
        self.verify(received_quantity='200', damaged_quantity='200')
        v = self.line.verification
        self.manager.post(f'{URL}/verifications/{v.id}/accept-discrepancy/', {'reason': 'All damaged; rejected'}, format='json')
        self.transition('verification-pending')
        self.assertEqual(self.transition('mark-unloaded').status_code, 200)

    def test_accepted_quantity_cannot_drop_below_packaged(self):
        self.verify()
        inv.create_packages(self.line, [{'quantity': Decimal('150')}], self.fx.users['RECEIVING'])
        resp = self.verify(accepted_quantity='100')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'below_packaged_quantity')


class UnidentifiedMaterialTests(UnloadingTestCase):
    def test_unidentified_line_needs_identified_material_for_packaging(self):
        receipt = self.fx.receipt_in_unloading(lines=[{'material': None, 'material_name_declared': 'Unknown powder'}])
        line = receipt.invoice_lines.get()
        body = self.verify(line=line).json()
        self.assertEqual(body['discrepancy_status'], 'UNIDENTIFIED')

        body = self.verify(line=line, identified_material=self.fx.material.pk).json()
        self.assertEqual(body['verification_status'], 'VERIFIED')
        pkgs = inv.create_packages(InvoiceLine.objects.get(pk=line.pk), [{'quantity': Decimal('200')}],
                                   self.fx.users['RECEIVING'])
        self.assertEqual(pkgs[0].material, self.fx.material)
        line.refresh_from_db()
        self.assertIsNone(line.material)  # invoice record untouched
