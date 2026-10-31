"""
Phase 7 — reports & exports, management dashboard, global search, audit API, scheduled
alerts, hardening and query-count (N+1) checks. Covers spec test item 15 (dashboard
totals reconcile with inventory records) and exports respecting permissions / filters.
"""
import csv
import io
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from openpyxl import load_workbook
from rest_framework.test import APIClient

from apps.core.models import AuditEvent
from apps.core.testing import client_for
from apps.inventory.models import Package, MaterialIssue, MaterialIssueLine
from apps.laboratory.models import LabSample
from apps.notifications.models import Notification
from apps.receiving.models import GoodsReceipt
from apps.users.models import User

R = '/api/v1/reports'


class DemoDataTestCase(TestCase):
    """Realistic data built through the real services (seed_demo --with-sample-flow)."""

    @classmethod
    def setUpTestData(cls):
        call_command('seed_demo', '--with-sample-flow', '--allow-non-debug', stdout=StringIO())

    def as_(self, role):
        return client_for(User.objects.get(username=f'demo_{role.lower()}'))


class ReportAccessTests(DemoDataTestCase):
    def test_reports_listed_by_role(self):
        keys = lambda role: {r['key'] for r in self.as_(role).get(f'{R}/').json()}
        self.assertIn('audit-events', keys('AUDITOR'))
        self.assertNotIn('audit-events', keys('WAREHOUSE'))
        self.assertNotIn('issue-register', keys('GATE'))
        self.assertIn('inward-register', keys('GATE'))
        self.assertEqual(keys('FACTORY'), set())

    def test_unknown_and_forbidden(self):
        self.assertEqual(self.as_('MANAGER').get(f'{R}/nope/').status_code, 404)
        self.assertEqual(self.as_('GATE').get(f'{R}/issue-register/').status_code, 403)
        self.assertEqual(self.as_('GATE').get(f'{R}/issue-register/', {'export': 'csv'}).status_code, 403)


class ReportContentTests(DemoDataTestCase):
    def test_inward_register_with_filters(self):
        body = self.as_('MANAGER').get(f'{R}/inward-register/').json()
        self.assertEqual(body['count'], GoodsReceipt.objects.count())
        self.assertEqual(body['columns'][0], 'Receipt')
        sup = GoodsReceipt.objects.get(invoice_number='DEMO-INV-B').supplier_id
        filtered = self.as_('MANAGER').get(f'{R}/inward-register/', {'supplier': sup}).json()
        self.assertEqual(filtered['count'], 1)
        future = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.as_('MANAGER').get(f'{R}/inward-register/', {'date_from': future}).json()['count'], 0)

    def test_current_stock_reconciles_with_records(self):
        body = self.as_('AUDITOR').get(f'{R}/current-stock/', {'page_size': 200}).json()
        qty_col = body['columns'].index('Quantity')
        self.assertEqual(body['count'], Package.objects.in_warehouse().count())
        self.assertEqual(sum(Decimal(r[qty_col]) for r in body['results']),
                         sum(p.current_quantity for p in Package.objects.in_warehouse()))
        avail = self.as_('AUDITOR').get(f'{R}/current-stock/', {'available': 'true'}).json()
        self.assertEqual(avail['count'], Package.objects.available().count())

    def test_lab_and_issue_registers(self):
        lab = self.as_('LAB_REVIEWER').get(f'{R}/lab-register/').json()
        self.assertEqual(lab['count'], 12)  # three tests on each of the four DEMO-LOT-A1 bags (one sample per bag)
        decisions = self.as_('LAB_REVIEWER').get(f'{R}/qc-decisions/').json()
        self.assertEqual(decisions['results'][0][decisions['columns'].index('Decision')], 'Released / Approved')
        issues = self.as_('WAREHOUSE').get(f'{R}/issue-register/').json()
        self.assertEqual(issues['count'], MaterialIssueLine.objects.count())
        self.assertEqual(self.as_('WAREHOUSE').get(f'{R}/issue-register/', {'overrides_only': 'true'}).json()['count'], 0)

    def test_reconciliation_and_ageing(self):
        rec = self.as_('MANAGER').get(f'{R}/receipt-reconciliation/').json()
        self.assertIn('Short', rec['columns'])
        self.assertEqual(self.as_('MANAGER').get(f'{R}/receipt-reconciliation/', {'discrepant_only': 'true'}).json()['count'], 0)
        ageing = self.as_('MANAGER').get(f'{R}/stock-ageing/').json()
        self.assertEqual(ageing['columns'][3:], ['0-30 days', '31-60 days', '61-90 days', '91+ days'])

    def test_package_lifecycle_report(self):
        self.assertEqual(self.as_('MANAGER').get(f'{R}/package-lifecycle/').status_code, 400)
        pkg = Package.objects.filter(batch_number='DEMO-LOT-A1').first()
        body = self.as_('MANAGER').get(f'{R}/package-lifecycle/', {'package': pkg.package_id.lower()}).json()
        self.assertGreater(body['count'], 10)

    def test_audit_events_report_filter(self):
        body = self.as_('AUDITOR').get(f'{R}/audit-events/', {'entity_type': 'QualityDisposition'}).json()
        self.assertEqual(body['count'], 4)  # one release decision per bag


class ExportTests(DemoDataTestCase):
    def test_csv_matches_json_and_is_audited(self):
        resp = self.as_('MANAGER').get(f'{R}/current-stock/', {'export': 'csv'})
        self.assertEqual(resp['Content-Type'], 'text/csv; charset=utf-8')
        self.assertIn('attachment; filename="current-stock-', resp['Content-Disposition'])
        text = b''.join(resp.streaming_content).decode('utf-8-sig')
        rows = list(csv.reader(io.StringIO(text)))
        self.assertEqual(rows[0][0], 'Package')
        self.assertEqual(len(rows) - 1, Package.objects.in_warehouse().count())
        event = AuditEvent.objects.get(action='DOWNLOAD', entity_type='Report', entity_id='current-stock')
        self.assertEqual(event.extra['format'], 'csv')

    def test_csv_respects_filters(self):
        sup = GoodsReceipt.objects.get(invoice_number='DEMO-INV-B').supplier_id
        resp = self.as_('MANAGER').get(f'{R}/inward-register/', {'export': 'csv', 'supplier': sup})
        rows = list(csv.reader(io.StringIO(b''.join(resp.streaming_content).decode('utf-8-sig'))))
        self.assertEqual(len(rows), 2)

    def test_xlsx(self):
        resp = self.as_('MANAGER').get(f'{R}/issue-register/', {'export': 'xlsx'})
        wb = load_workbook(io.BytesIO(resp.content))
        ws = wb.active
        self.assertEqual(ws.cell(1, 1).value, 'Issue')
        self.assertEqual(ws.max_row - 1, MaterialIssueLine.objects.count())

    def test_xlsx_row_limit(self):
        with mock.patch('apps.reports.views.XLSX_MAX_ROWS', 1):
            resp = self.as_('MANAGER').get(f'{R}/current-stock/', {'export': 'xlsx'})
        self.assertEqual(resp.json()['error'], 'export_too_large')

    def test_invalid_export(self):
        self.assertEqual(self.as_('MANAGER').get(f'{R}/current-stock/', {'export': 'pdf'}).status_code, 400)


class ManagementDashboardTests(DemoDataTestCase):
    def test_roles(self):
        self.assertEqual(self.as_('WAREHOUSE').get(f'{R}/management-dashboard/').status_code, 403)
        self.assertEqual(self.as_('AUDITOR').get(f'{R}/management-dashboard/').status_code, 200)

    def test_figures_reconcile_with_records(self):
        d = self.as_('MANAGER').get(f'{R}/management-dashboard/', {'days': 7}).json()
        self.assertEqual(d['inward']['receipts'], GoodsReceipt.objects.exclude(status='CANCELLED').count())
        self.assertEqual(sum(x['receipts'] for x in d['inward']['by_day']), d['inward']['receipts'])
        self.assertEqual(d['stock']['packages_in_warehouse'], Package.objects.in_warehouse().count())
        self.assertEqual(sum(d['stock']['ageing_package_counts'].values()), d['stock']['packages_in_warehouse'])
        self.assertEqual(d['stock']['available_packages'], Package.objects.available().count())
        self.assertEqual(d['issues']['issues'], MaterialIssue.objects.count())
        self.assertEqual(d['quality']['decisions'], {'RELEASED': 4})
        self.assertEqual(d['quality']['test_pass_rate_percent'], 100.0)
        self.assertIsNotNone(d['flow']['avg_gate_to_putaway_hours'])
        self.assertEqual(d['reconciliation']['ledger_mismatches'], 0)


class GlobalSearchTests(DemoDataTestCase):
    def search(self, role, q):
        return self.as_(role).get('/api/v1/search/', {'q': q})

    def test_finds_every_kind_of_reference(self):
        receipt = GoodsReceipt.objects.get(invoice_number='DEMO-INV-A')
        pkg = Package.objects.filter(batch_number='DEMO-LOT-A1').first()
        sample = LabSample.objects.first()
        issue = MaterialIssue.objects.get()
        cases = [(receipt.receipt_number, 'receipts'), (receipt.vehicle_number, 'receipts'),
                 ('DEMO-INV-A', 'receipts'), (pkg.package_id, 'packages'), ('DEMO-LOT-A1', 'packages'),
                 (f'http://localhost:3000/scan/{pkg.scan_token}', 'packages'), (sample.sample_id, 'samples'),
                 (issue.issue_number, 'issues'), ('DEMO-PO-1001', 'issues'), ('DEMO-MAT-001', 'materials')]
        for q, group in cases:
            self.assertTrue(self.search('MANAGER', q).json()['results'][group], (q, group))

    def test_factory_scoping_and_validation(self):
        body = self.search('FACTORY', 'DEMO').json()['results']
        self.assertNotIn('receipts', body)
        self.assertEqual(len(body['issues']), 1)  # their own
        other = User.objects.create_user('other_fac', 'o@x.invalid', 'x', role='FACTORY')
        self.assertEqual(client_for(other).get('/api/v1/search/', {'q': 'DEMO-PO'}).json()['results']['issues'], [])
        self.assertEqual(self.search('MANAGER', 'D').status_code, 400)


class AuditApiTests(DemoDataTestCase):
    def test_audit_trail_access(self):
        self.assertEqual(self.as_('WAREHOUSE').get('/api/v1/audit-events/').status_code, 403)
        body = self.as_('AUDITOR').get('/api/v1/audit-events/', {'action': 'OVERRIDE'}).json()
        self.assertEqual(body['count'], 0)
        self.assertGreater(self.as_('AUDITOR').get('/api/v1/audit-events/').json()['count'], 50)
        self.assertEqual(self.as_('AUDITOR').post('/api/v1/audit-events/', {}).status_code, 405)


class AlertTests(DemoDataTestCase):
    def run_alerts(self):
        out = StringIO()
        call_command('run_alerts', stdout=out)
        return out.getvalue()

    def test_alerts_reach_the_right_roles_once_per_day(self):
        pkg = Package.objects.available().first()
        Package.objects.filter(pk=pkg.pk).update(expiry_date=timezone.localdate() + timedelta(days=5))
        LabSample.objects.filter(status='TESTING').update(required_by=timezone.now() - timedelta(hours=1))
        Package.objects.filter(pk=Package.objects.in_warehouse().exclude(pk=pkg.pk).first().pk).update(
            current_quantity=Decimal('1'))  # ledger mismatch
        self.run_alerts()
        wh = User.objects.get(username='demo_warehouse')
        self.assertTrue(Notification.objects.filter(recipient=wh, title__startswith=f'{pkg.package_id} expiring').exists())
        self.assertTrue(Notification.objects.filter(recipient__username='demo_lab_tech', title__contains='overdue').exists())
        self.assertTrue(Notification.objects.filter(recipient__username='demo_admin', level='CRITICAL').exists())
        before = Notification.objects.count()
        self.assertIn('0 notification(s) sent', self.run_alerts())
        self.assertEqual(Notification.objects.count(), before)

    def test_job_failure_notifies_admin(self):
        with mock.patch('apps.notifications.management.commands.run_alerts.collect_alerts', side_effect=RuntimeError('boom')):
            with self.assertRaises(RuntimeError):
                self.run_alerts()
        self.assertTrue(Notification.objects.filter(recipient__username='demo_admin',
                                                    title='Scheduled job failed: run_alerts').exists())


class HardeningTests(TestCase):
    def test_request_id_generated_echoed_and_audited(self):
        user = User.objects.create_user('a', 'a@x.invalid', 'Test-pass-123!', role='ADMIN')
        client = client_for(user)
        resp = client.get('/api/v1/auth/me/')
        self.assertRegex(resp['X-Request-ID'], r'^[0-9a-f]{32}$')
        resp = client.post(f'/api/v1/auth/users/{user.pk}/set-role/', {'role': 'MANAGER'}, format='json',
                           HTTP_X_REQUEST_ID='trace-123')
        self.assertEqual(resp['X-Request-ID'], 'trace-123')
        self.assertEqual(AuditEvent.objects.get(entity_type='User').correlation_id, 'trace-123')
        resp = client.get('/api/v1/auth/me/', HTTP_X_REQUEST_ID='bad id with spaces')
        self.assertNotEqual(resp['X-Request-ID'], 'bad id with spaces')

    def test_login_is_throttled(self):
        User.objects.create_user('t', 't@x.invalid', 'Test-pass-123!', role='GATE')
        cache.clear()
        from rest_framework.settings import api_settings
        with mock.patch.dict(api_settings.DEFAULT_THROTTLE_RATES, {'login': '3/min'}), \
                mock.patch('rest_framework.throttling.ScopedRateThrottle.THROTTLE_RATES', {'login': '3/min'}):
            codes = [APIClient().post('/api/v1/auth/login/', {'username': 't', 'password': 'wrong'}, format='json').status_code
                     for _ in range(5)]
        cache.clear()
        self.assertEqual(codes[:3], [401, 401, 401])
        self.assertEqual(codes[3:], [429, 429])

    def test_weak_secret_refused_in_production(self):
        import subprocess
        import sys
        env = {'DEBUG': 'False', 'SECRET_KEY': 'short', 'PATH': '/usr/bin:/bin', 'DJANGO_SETTINGS_MODULE': 'config.settings'}
        proc = subprocess.run([sys.executable, '-c', 'import django; django.setup()'], env=env, capture_output=True,
                              text=True, cwd=str(__import__('pathlib').Path(__file__).resolve().parents[2]))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Set a strong SECRET_KEY', proc.stderr)


class QueryCountTests(TestCase):
    """List endpoints must not issue a query per row (N+1)."""

    def setUp(self):
        from apps.core.testing import Fixtures
        self.fx = Fixtures()

    def count(self, url, role='ADMIN'):
        client = client_for(self.fx.users[role])
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        self.assertEqual(resp.status_code, 200, resp.content)
        return len(ctx.captured_queries)

    def grow(self, n):
        from apps.inventory import services as inv
        for i in range(n):
            _, pkgs = self.fx.packaged_receipt(count=2, material=self.fx.material_noqc, invoice_number=f'Q{n}-{i}')
            for p in pkgs:
                inv.putaway_package(p, self.fx.shelf1, self.fx.users['WAREHOUSE'])

    def test_list_endpoints_are_constant_in_rows(self):
        urls = ['/api/v1/inventory/packages/', '/api/v1/receiving/goods-receipts/', '/api/v1/inventory/movements/',
                '/api/v1/laboratory/samples/', '/api/v1/inventory/issues/', '/api/v1/inventory/requests/',
                '/api/v1/reports/current-stock/']
        self.grow(1)
        small = {u: self.count(u) for u in urls}
        self.grow(3)
        large = {u: self.count(u) for u in urls}
        self.assertEqual(small, large, {u: (small[u], large[u]) for u in urls if small[u] != large[u]})


class UsageAnalysisTests(DemoDataTestCase):
    def test_roles_and_figures_match_records(self):
        self.assertEqual(self.as_('WAREHOUSE').get(f'{R}/usage-analysis/').status_code, 403)
        d = self.as_('MANAGER').get(f'{R}/usage-analysis/', {'days': 7}).json()
        self.assertEqual(d['users']['active_accounts'], User.objects.filter(is_active=True).count())
        acting = AuditEvent.objects.filter(actor__isnull=False).values('actor').distinct().count()
        self.assertEqual(d['users']['active_in_period'], acting)
        self.assertGreaterEqual(d['concurrency']['peak_people_in_one_hour'], 1)
        self.assertEqual(sum(d['concurrency']['actions_by_hour_of_day'].values()),
                         AuditEvent.objects.filter(actor__isnull=False).count())
        bags = d['volumes']['bags_created']
        self.assertEqual(bags['total'], Package.objects.filter(parent_package__isnull=True).count())
        self.assertEqual(bags['per_day_avg'], round(bags['total'] / 7, 2))
        self.assertEqual(d['volumes']['issues']['total'], MaterialIssue.objects.count())
        self.assertEqual(d['footprint']['bags_in_stock'], Package.objects.in_warehouse().count())
        self.assertNotIn('target', str(d).lower().replace('no targets', ''))
