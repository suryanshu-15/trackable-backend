from django.db import IntegrityError, transaction
from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import AuditEvent
from apps.core.testing import Fixtures, client_for
from apps.receiving.models import GoodsReceipt


class AuditImmutabilityTests(TestCase):
    def test_audit_event_cannot_be_updated(self):
        event = AuditEvent.objects.create(action='CREATE', entity_type='X', entity_id='1')
        event.reason = 'tampered'
        with self.assertRaises(ValueError):
            event.save()

    def test_new_audit_event_saves(self):
        AuditEvent.objects.create(action='CREATE', entity_type='X', entity_id='1')
        self.assertEqual(AuditEvent.objects.count(), 1)


class HealthAndErrorShapeTests(TestCase):
    def test_health_reports_database(self):
        resp = self.client.get('/health/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {'status': 'ok', 'database': True})

    def test_unauthenticated_api_returns_consistent_error_shape(self):
        resp = APIClient().get('/api/v1/receiving/goods-receipts/')
        self.assertEqual(resp.status_code, 401)
        self.assertEqual(set(resp.json()), {'error', 'message', 'details'})
        self.assertEqual(resp.json()['error'], 'unauthorized')


class DatabaseConstraintTests(TestCase):
    """Constraints hold even if application validation is bypassed."""

    def setUp(self):
        self.fx = Fixtures()

    def test_gross_less_than_tare_rejected_by_database(self):
        r = GoodsReceipt(
            supplier=self.fx.supplier, invoice_number='X', invoice_date='2026-01-01',
            vehicle_number='V', arrival_datetime='2026-01-01T10:00Z',
            gross_weight=100, tare_weight=200, weight_unit=self.fx.kg,
        )
        # bulk_create skips save()/clean(), so only the DB constraint can stop this
        with self.assertRaises(IntegrityError), transaction.atomic():
            GoodsReceipt.objects.bulk_create([r])


class MasterDataDeleteTests(TestCase):
    def setUp(self):
        self.fx = Fixtures()

    def test_deleting_referenced_master_data_returns_409(self):
        admin = client_for(self.fx.users['ADMIN'])
        gate = client_for(self.fx.users['GATE'])
        resp = gate.post('/api/v1/receiving/goods-receipts/', self.fx.receipt_payload(), format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = admin.delete(f'/api/v1/master/suppliers/{self.fx.supplier.pk}/')
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['error'], 'conflict')


class SeedDemoTests(TestCase):
    def test_seed_with_sample_flow_is_idempotent_and_consistent(self):
        from io import StringIO
        from django.core.management import call_command
        from apps.inventory.models import Package
        from apps.inventory.services import reconciliation_issues
        call_command('seed_demo', '--with-sample-flow', '--allow-non-debug', stdout=StringIO())
        call_command('seed_demo', '--with-sample-flow', '--allow-non-debug', stdout=StringIO())
        self.assertEqual(GoodsReceipt.objects.filter(invoice_number__startswith='DEMO-INV-').count(), 4)
        # 4 released packages, 250 kg of the oldest issued → 3 still available
        self.assertEqual(Package.objects.available().count(), 3)
        self.assertEqual(reconciliation_issues(), [])
