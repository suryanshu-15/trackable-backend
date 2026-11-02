"""
Phase 2 — Gate & receipt. Covers spec test items 1, 2, 3 (lines), 14 (roles) and 16 (uploads).
"""
import io
import shutil
import tempfile
import zipfile
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.core.models import AuditEvent, SystemConfiguration
from apps.core.testing import Fixtures, client_for
from apps.receiving.models import GoodsReceipt, ReceiptStatus

URL = '/api/v1/receiving/goods-receipts/'
LINES = '/api/v1/receiving/invoice-lines/'


class ReceivingTestCase(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        self.gate = client_for(self.fx.users['GATE'])
        self.admin = client_for(self.fx.users['ADMIN'])

    def create_receipt(self, client=None, **overrides):
        resp = (client or self.gate).post(URL, self.fx.receipt_payload(**overrides), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def add_line(self, receipt_id, client=None, **overrides):
        resp = (client or self.gate).post(LINES, self.fx.line_payload(receipt_id, **overrides), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def post(self, receipt_id, action, data=None, client=None):
        return (client or self.gate).post(f'{URL}{receipt_id}/{action}/', data or {}, format='json')


class ReceiptCreationTests(ReceivingTestCase):
    def test_create_generates_unique_reference_and_draft(self):
        r1 = self.create_receipt()
        r2 = self.create_receipt(invoice_number='INV-002')
        self.assertTrue(r1['receipt_number'].startswith('GR-'))
        self.assertNotEqual(r1['receipt_number'], r2['receipt_number'])
        self.assertEqual(r1['status'], ReceiptStatus.DRAFT)

    def test_create_is_audited_with_actor(self):
        r = self.create_receipt()
        event = AuditEvent.objects.get(entity_type='GoodsReceipt', entity_id=r['id'], action='CREATE')
        self.assertEqual(event.actor, self.fx.users['GATE'])

    def test_duplicate_supplier_invoice_requires_acknowledgement(self):
        first = self.create_receipt()
        resp = self.gate.post(URL, self.fx.receipt_payload(invoice_number='inv-001'), format='json')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['error'], 'duplicate_invoice')
        self.assertEqual(resp.json()['details']['duplicate_receipts'], [first['receipt_number']])

        resp = self.gate.post(f'{URL}?acknowledge_duplicate=true', self.fx.receipt_payload(), format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()['duplicate_warning'], [first['receipt_number']])
        event = AuditEvent.objects.get(entity_id=resp.json()['id'], action='CREATE')
        self.assertEqual(event.extra, {'duplicate_acknowledged': [first['receipt_number']]})

    def test_same_invoice_number_from_another_supplier_is_not_duplicate(self):
        self.create_receipt()
        self.create_receipt(supplier=self.fx.supplier2.pk)

    def test_same_invoice_number_different_date_is_not_duplicate(self):
        self.create_receipt()
        self.create_receipt(invoice_date='2026-10-02')

    def test_cancelled_receipt_is_not_a_duplicate(self):
        r = self.create_receipt()
        self.assertEqual(self.post(r['id'], 'cancel', {'reason': 'wrong truck'}, client=self.admin).status_code, 200)
        self.create_receipt()

    def test_duplicate_key_is_configurable(self):
        SystemConfiguration.objects.update_or_create(key='receipt.duplicate_check_fields', defaults={'value': '["supplier", "invoice_number"]'})
        self.create_receipt()
        resp = self.gate.post(URL, self.fx.receipt_payload(invoice_date='2026-10-05'), format='json')
        self.assertEqual(resp.status_code, 409)


class WeightTests(ReceivingTestCase):
    def test_net_weight_is_gross_minus_tare(self):
        r = self.create_receipt()
        self.assertEqual(Decimal(r['net_weight']), Decimal('10000'))

    def test_net_weight_is_read_only(self):
        r = self.create_receipt(net_weight='1')
        self.assertEqual(Decimal(r['net_weight']), Decimal('10000'))

    def test_gross_below_tare_rejected(self):
        resp = self.gate.post(URL, self.fx.receipt_payload(gross_weight='100', tare_weight='200'), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('tare_weight', resp.json()['details'])

    def test_weights_require_unit(self):
        resp = self.gate.post(URL, self.fx.receipt_payload(weight_unit=None), format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('weight_unit', resp.json()['details'])

    def test_zero_tare_computes_net(self):
        r = self.create_receipt(tare_weight='0')
        self.assertEqual(Decimal(r['net_weight']), Decimal('15000'))

    def test_draft_edit_recomputes_net(self):
        r = self.create_receipt()
        resp = self.gate.patch(f"{URL}{r['id']}/", {'tare_weight': '6000'}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Decimal(resp.json()['net_weight']), Decimal('9000'))

    def test_registered_receipt_cannot_be_edited_directly(self):
        r = self.create_receipt()
        self.post(r['id'], 'register')
        resp = self.gate.patch(f"{URL}{r['id']}/", {'tare_weight': '6000'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'receipt_locked')

    def test_weight_correction_requires_reason_and_is_audited(self):
        r = self.create_receipt()
        self.post(r['id'], 'register')
        resp = self.post(r['id'], 'correct-weights', {'tare_weight': '5500'})
        self.assertEqual(resp.status_code, 400)

        resp = self.post(r['id'], 'correct-weights', {'tare_weight': '5500', 'reason': 'Weighbridge re-read'})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(Decimal(resp.json()['net_weight']), Decimal('9500'))
        event = AuditEvent.objects.filter(entity_id=r['id'], extra__kind='WEIGHT_CORRECTION').get()
        self.assertEqual(event.reason, 'Weighbridge re-read')
        self.assertEqual(Decimal(event.before_state['net_weight']), Decimal('10000'))
        self.assertEqual(Decimal(event.after_state['net_weight']), Decimal('9500'))

    def test_weight_correction_still_enforces_gross_gte_tare(self):
        r = self.create_receipt()
        resp = self.post(r['id'], 'correct-weights', {'tare_weight': '20000', 'reason': 'x'})
        self.assertEqual(resp.status_code, 400)

    def test_weight_correction_role(self):
        r = self.create_receipt()
        resp = self.post(r['id'], 'correct-weights', {'tare_weight': '5500', 'reason': 'x'},
                         client=client_for(self.fx.users['WAREHOUSE']))
        self.assertEqual(resp.status_code, 403)

    def test_weight_discrepancy_against_declared_line_weights(self):
        r = self.create_receipt()
        self.add_line(r['id'], declared_weight='9000')
        d = self.gate.get(f"{URL}{r['id']}/").json()['weight_discrepancy']
        self.assertEqual(Decimal(d['difference']), Decimal('1000'))
        self.assertTrue(d['exceeds_tolerance'])


class InvoiceLineTests(ReceivingTestCase):
    def test_multiple_lines_with_different_units(self):
        r = self.create_receipt()
        l1 = self.add_line(r['id'])
        l2 = self.add_line(r['id'], material=None, material_name_declared='Unlisted steel rod',
                           declared_quantity='50', declared_unit=self.fx.pcs.pk, declared_weight=None)
        self.assertEqual((l1['line_number'], l2['line_number']), (1, 2))
        detail = self.gate.get(f"{URL}{r['id']}/").json()
        self.assertEqual(len(detail['invoice_lines']), 2)
        self.assertEqual(l1['material_name_declared'], 'Test Material')

    def test_line_requires_material_or_name(self):
        r = self.create_receipt()
        resp = self.gate.post(LINES, self.fx.line_payload(r['id'], material=None), format='json')
        self.assertEqual(resp.status_code, 400)

    def test_line_requires_positive_quantity_and_unit(self):
        r = self.create_receipt()
        self.assertEqual(self.gate.post(LINES, self.fx.line_payload(r['id'], declared_quantity='0'), format='json').status_code, 400)
        payload = self.fx.line_payload(r['id'])
        del payload['declared_unit']
        self.assertEqual(self.gate.post(LINES, payload, format='json').status_code, 400)

    def test_duplicate_line_number_rejected(self):
        r = self.create_receipt()
        self.add_line(r['id'], line_number=3)
        resp = self.gate.post(LINES, self.fx.line_payload(r['id'], line_number=3), format='json')
        self.assertEqual(resp.status_code, 400)

    def test_lines_locked_after_submission(self):
        r = self.create_receipt()
        line = self.add_line(r['id'])
        self.post(r['id'], 'register')
        self.add_line(r['id'])  # still allowed when GATE_REGISTERED
        self.assertEqual(self.post(r['id'], 'submit-unloading').status_code, 200)
        resp = self.gate.patch(f"{LINES}{line['id']}/", {'declared_quantity': '1'}, format='json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.gate.delete(f"{LINES}{line['id']}/").status_code, 400)
        resp = self.gate.post(LINES, self.fx.line_payload(r['id']), format='json')
        self.assertEqual(resp.status_code, 400)

    def test_line_cannot_move_to_another_receipt(self):
        r1 = self.create_receipt()
        r2 = self.create_receipt(invoice_number='INV-2')
        line = self.add_line(r1['id'])
        self.gate.patch(f"{LINES}{line['id']}/", {'receipt': r2['id']}, format='json')
        self.assertEqual(self.gate.get(f"{LINES}{line['id']}/").json()['receipt'], r1['id'])

    def test_line_changes_appear_in_timeline(self):
        r = self.create_receipt()
        line = self.add_line(r['id'])
        self.gate.patch(f"{LINES}{line['id']}/", {'declared_quantity': '150'}, format='json')
        self.gate.delete(f"{LINES}{line['id']}/")
        timeline = self.gate.get(f"{URL}{r['id']}/timeline/").json()
        self.assertEqual(
            [(e['entity_type'], e['action']) for e in timeline],
            [('GoodsReceipt', 'CREATE'), ('InvoiceLine', 'CREATE'), ('InvoiceLine', 'UPDATE'), ('InvoiceLine', 'DELETE')],
        )
        self.assertEqual(timeline[2]['before_state']['declared_quantity'], '200.0000')


class TransitionTests(ReceivingTestCase):
    def test_happy_path_to_awaiting_unloading(self):
        r = self.create_receipt()
        self.add_line(r['id'])
        resp = self.post(r['id'], 'register')
        self.assertEqual(resp.json()['status'], 'GATE_REGISTERED')
        self.assertIsNotNone(resp.json()['gate_registered_at'])
        resp = self.post(r['id'], 'submit-unloading')
        self.assertEqual(resp.json()['status'], 'AWAITING_UNLOADING')
        events = AuditEvent.objects.filter(entity_id=r['id'], action='TRANSITION').order_by('timestamp')
        self.assertEqual([e.after_state['status'] for e in events], ['GATE_REGISTERED', 'AWAITING_UNLOADING'])

    def test_submit_requires_lines(self):
        r = self.create_receipt()
        self.post(r['id'], 'register')
        resp = self.post(r['id'], 'submit-unloading')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'no_invoice_lines')

    def test_invalid_source_state(self):
        r = self.create_receipt()
        resp = self.post(r['id'], 'submit-unloading')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()['error'], 'invalid_transition')

    def test_cancel_needs_a_reason_and_is_allowed_to_gate_and_admin_before_unloading(self):
        r = self.create_receipt()
        self.assertEqual(self.post(r['id'], 'cancel').status_code, 400)  # no reason
        self.assertEqual(self.post(r['id'], 'cancel', {'reason': 'Entered by mistake'}).status_code, 200)  # gate
        r2 = self.create_receipt(invoice_number='INV-CANCEL-2')
        self.assertEqual(self.post(r2['id'], 'cancel', {'reason': 'Wrong truck'}, client=self.admin).status_code, 200)
        from apps.core.testing import client_for
        receiving = client_for(self.fx.users['RECEIVING'])
        r3 = self.create_receipt(invoice_number='INV-CANCEL-3')
        self.assertEqual(self.post(r3['id'], 'cancel', {'reason': 'x'}, client=receiving).status_code, 403)

    def test_allowed_transitions_depend_on_role(self):
        r = self.create_receipt()
        self.assertEqual(sorted(self.gate.get(f"{URL}{r['id']}/").json()['allowed_transitions']), ['cancel', 'register_at_gate'])
        admin_view = self.admin.get(f"{URL}{r['id']}/").json()['allowed_transitions']
        self.assertEqual(sorted(admin_view), ['cancel', 'register_at_gate'])

    def test_receipts_cannot_be_deleted(self):
        r = self.create_receipt()
        self.assertEqual(self.admin.delete(f"{URL}{r['id']}/").status_code, 405)


class RoleAccessTests(ReceivingTestCase):
    def test_only_gate_and_admin_create_receipts(self):
        for role in ('RECEIVING', 'WAREHOUSE', 'LAB_TECH', 'FACTORY', 'AUDITOR', 'MANAGER'):
            resp = client_for(self.fx.users[role]).post(URL, self.fx.receipt_payload(), format='json')
            self.assertEqual(resp.status_code, 403, role)

    def test_factory_role_cannot_read_receipts(self):
        r = self.create_receipt()
        factory = client_for(self.fx.users['FACTORY'])
        self.assertEqual(factory.get(URL).status_code, 403)
        self.assertEqual(factory.get(f"{URL}{r['id']}/").status_code, 403)

    def test_auditor_reads_but_cannot_transition(self):
        r = self.create_receipt()
        auditor = client_for(self.fx.users['AUDITOR'])
        self.assertEqual(auditor.get(f"{URL}{r['id']}/").status_code, 200)
        self.assertEqual(self.post(r['id'], 'register', client=auditor).status_code, 403)

    def test_receiving_cannot_register_at_gate(self):
        r = self.create_receipt()
        self.assertEqual(self.post(r['id'], 'register', client=client_for(self.fx.users['RECEIVING'])).status_code, 403)


class SearchAndDashboardTests(ReceivingTestCase):
    def test_search_by_vehicle_supplier_material_batch(self):
        r = self.create_receipt(vehicle_number='OD05ZZ9999')
        self.add_line(r['id'], batch_number='LOT-XYZ')
        self.create_receipt(invoice_number='OTHER', vehicle_number='KA01AA0001')
        for term in ('OD05ZZ', 'Supplier One', 'MAT-1', 'LOT-XYZ', r['receipt_number']):
            resp = self.gate.get(URL, {'search': term})
            self.assertEqual(resp.json()['count'], 1 if term != 'Supplier One' else 2, term)

    def test_filter_by_status_and_material(self):
        r = self.create_receipt()
        self.add_line(r['id'])
        self.create_receipt(invoice_number='INV-9')
        self.post(r['id'], 'register')
        self.assertEqual(self.gate.get(URL, {'status': 'GATE_REGISTERED'}).json()['count'], 1)
        self.assertEqual(self.gate.get(URL, {'material': self.fx.material.pk}).json()['count'], 1)

    def test_dashboard_reflects_persisted_state(self):
        r = self.create_receipt()
        self.create_receipt(invoice_number='INV-2')
        self.add_line(r['id'], declared_weight='5000')
        self.post(r['id'], 'register')
        data = self.gate.get(f'{URL}dashboard/').json()
        self.assertEqual(data['today_receipts'], 2)
        self.assertEqual(data['by_status']['DRAFT'], 1)
        self.assertEqual(data['by_status']['GATE_REGISTERED'], 1)
        self.assertEqual(data['trucks_on_site'], 1)
        self.assertEqual(data['awaiting_line_entry'], 1)
        self.assertEqual(data['weight_discrepancy_receipts'], [r['receipt_number']])
        self.assertEqual(sum(data['by_status'].values()), GoodsReceipt.objects.count())


def _png():
    return b'\x89PNG\r\n\x1a\n' + b'\x00' * 64


def _xlsx():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('[Content_Types].xml', '<Types/>')
        zf.writestr('xl/workbook.xml', '<workbook/>')
    return buf.getvalue()


class DocumentTests(ReceivingTestCase):
    def setUp(self):
        super().setUp()
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()
        self.receipt = self.create_receipt()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)
        super().tearDown()

    def upload(self, name, content, content_type='application/octet-stream', client=None, doc_type='INVOICE'):
        return (client or self.gate).post(
            f"{URL}{self.receipt['id']}/documents/",
            {'file': SimpleUploadedFile(name, content, content_type=content_type), 'document_type': doc_type},
            format='multipart',
        )

    def test_valid_pdf_upload_and_protected_download(self):
        resp = self.upload('invoice.pdf', b'%PDF-1.7\n%test', content_type='text/html')
        self.assertEqual(resp.status_code, 201, resp.content)
        doc = resp.json()
        self.assertEqual(doc['mime_type'], 'application/pdf')  # sniffed, not browser-supplied
        self.assertNotIn('file', doc)
        self.assertTrue(doc['download_url'].endswith('/download/'))

        dl = client_for(self.fx.users['AUDITOR']).get(doc['download_url'])
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(b''.join(dl.streaming_content), b'%PDF-1.7\n%test')
        self.assertIn('invoice.pdf', dl['Content-Disposition'])
        self.assertTrue(AuditEvent.objects.filter(entity_id=doc['id'], action='DOWNLOAD').exists())

        self.assertEqual(client_for(self.fx.users['FACTORY']).get(doc['download_url']).status_code, 403)
        from rest_framework.test import APIClient
        self.assertEqual(APIClient().get(doc['download_url']).status_code, 401)

    def test_media_is_not_served_publicly(self):
        self.upload('invoice.pdf', b'%PDF-1.7\n')
        self.assertEqual(self.client.get('/media/').status_code, 404)

    def test_extension_content_mismatch_rejected(self):
        resp = self.upload('invoice.pdf', b'<html><script>alert(1)</script></html>', content_type='application/pdf')
        self.assertEqual(resp.status_code, 400)
        self.assertIn('does not match', resp.json()['message'])

    def test_disallowed_extension_rejected(self):
        self.assertEqual(self.upload('run.exe', b'MZ\x90\x00').status_code, 400)
        self.assertEqual(self.upload('page.html', b'<html></html>').status_code, 400)

    def test_png_and_ooxml_accepted(self):
        self.assertEqual(self.upload('slip.png', _png(), doc_type='WEIGHMENT_SLIP').status_code, 201)
        self.assertEqual(self.upload('list.xlsx', _xlsx(), doc_type='PACKING_LIST').status_code, 201)
        self.assertEqual(self.upload('fake.xlsx', b'PK\x03\x04garbage').status_code, 400)

    @override_settings(MAX_UPLOAD_SIZE_MB=0)
    def test_oversize_rejected(self):
        self.assertEqual(self.upload('invoice.pdf', b'%PDF-1.7\n').status_code, 400)

    def test_upload_role_and_audit(self):
        self.assertEqual(self.upload('i.pdf', b'%PDF-1.7', client=client_for(self.fx.users['AUDITOR'])).status_code, 403)
        doc = self.upload('i.pdf', b'%PDF-1.7').json()
        self.assertTrue(AuditEvent.objects.filter(entity_id=doc['id'], action='UPLOAD').exists())
        timeline = self.gate.get(f"{URL}{self.receipt['id']}/timeline/").json()
        self.assertIn('ReceiptDocument', [e['entity_type'] for e in timeline])

    def test_stored_filename_does_not_reuse_client_name(self):
        from apps.receiving.models import ReceiptDocument
        self.upload('../../etc/passwd.pdf', b'%PDF-1.7')
        doc = ReceiptDocument.objects.get()
        self.assertNotIn('passwd', doc.file.name)
        self.assertTrue(doc.file.name.startswith(f"receipts/{self.receipt['receipt_number']}/INVOICE-"))
