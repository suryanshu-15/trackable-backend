"""
Phase 6 — material requests, FIFO/FEFO, reservations, issue, returns, disposals, adjustments.
Covers spec test items 9, 10, 12, 13 and 14 (object-level permissions).
"""
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.models import AuditEvent, SystemConfiguration
from apps.core.testing import Fixtures, client_for, make_user
from apps.inventory import issuing, services as inv
from apps.inventory.models import (
    Package, PackageStatus, QCStatus, MaterialRequest, RequestStatus, StockReservation, MaterialIssue,
    MaterialIssueLine,
)
from apps.master.models import Factory, Department
from apps.notifications.models import Notification

API = '/api/v1/inventory'
D = Decimal


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


class IssueTestCase(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        self.factory = Factory.objects.create(code='F1', name='Factory 1')
        self.dept = Department.objects.create(factory=self.factory, code='MIX', name='Mixing')
        self.other_factory = Factory.objects.create(code='F2', name='Factory 2')
        self.wh, self.mgr = client_for(self.fx.users['WAREHOUSE']), client_for(self.fx.users['MANAGER'])
        self.fac = client_for(self.fx.users['FACTORY'])
        self.mat = self.fx.material_noqc  # QC not required: stored stock is directly available
        # Three receipts: oldest first
        self.old = self.stock(days_ago=10, invoice='OLD')
        self.mid = self.stock(days_ago=5, invoice='MID')
        self.new = self.stock(days_ago=1, invoice='NEW')

    def stock(self, days_ago, invoice, count=1, each='100', material=None):
        _, pkgs = self.fx.packaged_receipt(count=count, each=each, material=material or self.mat, invoice_number=invoice,
                                           arrival_datetime=timezone.now() - timedelta(days=days_ago))
        for p in pkgs:
            inv.putaway_package(p, self.fx.shelf1, self.fx.users['WAREHOUSE'])
        return [fresh(p) for p in pkgs] if count > 1 else fresh(pkgs[0])

    def request(self, qty='150', client=None, **extra):
        payload = {'factory': self.factory.pk, 'department': self.dept.pk, 'production_order': 'PO-77',
                   'lines': [{'material': self.mat.pk, 'quantity': qty, 'unit': self.fx.kg.pk}], **extra}
        return (client or self.fac).post(f'{API}/requests/', payload, format='json')

    def issue(self, lines, client=None, key=None, **extra):
        payload = {'factory': self.factory.pk, 'received_by_name': 'Line supervisor',
                   'lines': [{'package': str(p.pk), 'quantity': str(q)} for p, q in lines], **extra}
        headers = {'HTTP_IDEMPOTENCY_KEY': key} if key else {}
        return (client or self.wh).post(f'{API}/issues/', payload, format='json', **headers)


class RequestTests(IssueTestCase):
    def test_factory_raises_request(self):
        resp = self.request()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body['request_number'].startswith('REQ-'))
        self.assertEqual((body['status'], body['lines'][0]['remaining_quantity']), ('SUBMITTED', '150.0000'))
        self.assertTrue(Notification.objects.filter(recipient=self.fx.users['WAREHOUSE'], entity_type='MaterialRequest').exists())

    def test_validation_and_roles(self):
        dup = {'factory': self.factory.pk, 'lines': [{'material': self.mat.pk, 'quantity': '1', 'unit': self.fx.kg.pk}] * 2}
        self.assertEqual(self.fac.post(f'{API}/requests/', dup, format='json').status_code, 400)
        other_dept = Department.objects.create(factory=self.other_factory, code='X', name='X')
        self.assertEqual(self.request(department=other_dept.pk).status_code, 400)
        self.assertEqual(self.request(client=client_for(self.fx.users['GATE'])).status_code, 403)

    def test_factory_users_only_see_their_own_requests_and_issues(self):
        mine = self.request().json()
        other = client_for(make_user('FACTORY', username='other_factory_user'))
        self.assertEqual(other.get(f"{API}/requests/{mine['id']}/").status_code, 404)
        self.assertEqual(other.get(f'{API}/requests/').json()['count'], 0)
        self.assertEqual(self.wh.get(f'{API}/requests/').json()['count'], 1)
        issue = self.issue([(self.old, 100)], material_request=mine['id']).json()
        self.assertEqual(self.fac.get(f"{API}/issues/{issue['id']}/").status_code, 200)
        self.assertEqual(other.get(f"{API}/issues/{issue['id']}/").status_code, 404)

    def test_cancel(self):
        req = self.request().json()
        url = f"{API}/requests/{req['id']}/cancel/"
        self.assertEqual(self.fac.post(url, {}, format='json').status_code, 400)
        self.assertEqual(self.fac.post(url, {'reason': 'Order postponed'}, format='json').json()['status'], 'CANCELLED')


class FifoFefoTests(IssueTestCase):
    def suggest(self, qty):
        return self.wh.get(f'{API}/stock/fifo-suggestion/', {'material_id': self.mat.pk, 'quantity': qty}).json()

    def test_fifo_oldest_received_first(self):
        s = self.suggest('150')
        self.assertEqual(s['policy'], 'FIFO')
        self.assertEqual([(x['package_id'], x['quantity']) for x in s['suggestions']],
                         [(self.old.package_id, '100.0000'), (self.mid.package_id, '50.0000')])
        self.assertEqual(D(self.suggest('400')['shortfall']), D('100'))

    def test_fefo_earliest_expiry_first(self):
        today = timezone.localdate()
        Package.objects.filter(pk=self.old.pk).update(expiry_date=today + timedelta(days=200))
        Package.objects.filter(pk=self.new.pk).update(expiry_date=today + timedelta(days=20))
        s = self.suggest('150')
        self.assertEqual(s['policy'], 'FEFO')
        self.assertEqual([x['package_id'] for x in s['suggestions']], [self.new.package_id, self.old.package_id])

    def test_policy_can_be_forced(self):
        Package.objects.filter(pk=self.new.pk).update(expiry_date=timezone.localdate() + timedelta(days=20))
        SystemConfiguration.objects.update_or_create(key='issue.allocation_policy', defaults={'value': '"FIFO"'})
        self.assertEqual(self.suggest('100')['suggestions'][0]['package_id'], self.old.package_id)

    def test_blocked_stock_is_never_suggested(self):
        Package.objects.filter(pk=self.old.pk).update(is_on_hold=True)
        Package.objects.filter(pk=self.mid.pk).update(expiry_date=timezone.localdate() - timedelta(days=1))
        self.assertEqual([x['package_id'] for x in self.suggest('100')['suggestions']], [self.new.package_id])

    def test_whole_package_materials(self):
        self.mat.allow_partial_issue = False
        self.mat.save()
        s = self.suggest('150')
        self.assertEqual([(x['package_id'], x['quantity']) for x in s['suggestions']], [(self.old.package_id, '100.0000')])
        self.assertEqual(D(s['shortfall']), D('50'))


class AllocationTests(IssueTestCase):
    def test_allocation_reserves_oldest_and_blocks_others(self):
        req = self.request('150').json()
        resp = self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()['request']['status'], 'ALLOCATED')
        self.assertEqual(resp.json()['allocation'][0]['packages'], [self.old.package_id, self.mid.package_id])
        # Another issue cannot take the reserved quantity...
        resp = self.issue([(self.mid, 100)], override_reason='x', client=self.mgr)
        self.assertEqual(resp.json()['error'], 'issue_rejected')
        self.assertIn('(50.0000 reserved for other requests)', resp.json()['details']['packages'][0])
        # ...and the next free stock is the compliant choice (reserved stock is not "older available")
        self.assertEqual(self.issue([(self.mid, 50), (self.new, 10)]).status_code, 201)
        bal = self.wh.get(f'{API}/stock/balances/', {'group_by': 'material'}).json()['results']
        row = next(r for r in bal if r['material__code'] == self.mat.code)
        self.assertEqual((D(row['reserved_quantity']), D(row['available_quantity'])), (D('150'), D('90')))

    def test_reallocation_is_stable_and_release_frees(self):
        req = self.request('150').json()
        self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        self.assertEqual(StockReservation.objects.filter(is_active=True).count(), 2)
        self.assertEqual(self.wh.post(f"{API}/requests/{req['id']}/release-allocation/", {'reason': 'replan'}, format='json').json()['status'],
                         'SUBMITTED')
        self.assertFalse(StockReservation.objects.filter(is_active=True).exists())

    def test_partial_allocation_reports_shortfall(self):
        req = self.request('500').json()
        alloc = self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json').json()['allocation'][0]
        self.assertEqual((D(alloc['reserved']), D(alloc['shortfall'])), (D('300'), D('200')))

    def test_reserved_quantity_is_protected_from_split_disposal_and_adjustment(self):
        req = self.request('100').json()
        self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        self.assertEqual(self.wh.post(f'{API}/packages/{self.old.id}/split/', {'quantities': ['10'], 'reason': 'x'},
                                      format='json').status_code, 400)
        self.assertEqual(self.mgr.post(f'{API}/disposals/', {'package': str(self.old.pk), 'quantity': '10', 'reason': 'x'},
                                       format='json').status_code, 400)


class IssueTests(IssueTestCase):
    def test_issue_against_request_end_to_end(self):
        req = self.request('150').json()
        self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        resp = self.issue([(self.old, 100), (self.mid, 50)], material_request=req['id'])
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body['issue_number'].startswith('ISS-'))
        self.assertEqual((body['factory'], body['production_order'], body['fifo_override']), (self.factory.pk, 'PO-77', False))
        self.assertEqual([l['qc_status_at_issue'] for l in body['issue_lines']], ['NOT_REQUIRED', 'NOT_REQUIRED'])
        self.assertEqual(fresh(self.old).status, PackageStatus.ISSUED)
        self.assertEqual((fresh(self.mid).status, fresh(self.mid).current_quantity), (PackageStatus.STORED, D('50')))
        req_obj = MaterialRequest.objects.get(pk=req['id'])
        self.assertEqual(req_obj.status, RequestStatus.FULFILLED)
        self.assertFalse(StockReservation.objects.filter(is_active=True).exists())
        self.assertEqual(issuing.request_line_reconciliation(req_obj), [])
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_partial_fulfilment_then_close(self):
        req = self.request('150').json()
        self.issue([(self.old, 100)], material_request=req['id'])
        self.assertEqual(MaterialRequest.objects.get(pk=req['id']).status, RequestStatus.PARTIALLY_ISSUED)
        resp = self.issue([(self.mid, 60)], material_request=req['id'])
        self.assertEqual(resp.json()['error'], 'exceeds_request')
        self.assertEqual(self.wh.post(f"{API}/requests/{req['id']}/close/", {'reason': 'Order reduced'}, format='json').json()['status'], 'CLOSED')
        self.assertEqual(self.issue([(self.mid, 10)], material_request=req['id']).status_code, 400)

    def test_skipping_older_stock_needs_authorised_override(self):
        resp = self.issue([(self.new, 50)])
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.json()['error'], 'fifo_override_required')
        self.assertEqual(resp.json()['details']['violations'][0]['older_stock_not_used'],
                         [self.old.package_id, self.mid.package_id])
        self.assertEqual(self.issue([(self.new, 50)], override_reason='Line needs fresh lot').status_code, 403)
        resp = self.issue([(self.new, 50)], override_reason='Customer spec requires newest lot', client=self.mgr)
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body['fifo_override'])
        self.assertEqual(body['override_approved_by_name'], self.fx.users['MANAGER'].display_name)
        self.assertFalse(body['issue_lines'][0]['fifo_compliant'])
        self.assertTrue(AuditEvent.objects.filter(action='OVERRIDE', entity_id=body['id']).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.fx.users['ADMIN'], entity_type='MaterialIssue').exists())

    def test_override_can_be_forbidden_by_policy(self):
        SystemConfiguration.objects.update_or_create(key='issue.fifo_override_policy', defaults={'value': '"FORBID"'})
        resp = self.issue([(self.new, 50)], override_reason='x', client=self.mgr)
        self.assertEqual((resp.status_code, resp.json()['error']), (400, 'fifo_violation'))

    def test_partial_older_package_then_newer_is_a_violation(self):
        self.assertEqual(self.issue([(self.old, 50), (self.mid, 50)]).status_code, 409)
        self.assertEqual(self.issue([(self.old, 100), (self.mid, 50)]).status_code, 201)

    def test_packages_from_the_same_receipt_are_interchangeable(self):
        a, b = self.stock(days_ago=20, invoice='TWIN', count=2)
        self.assertEqual(self.issue([(b, 30)]).status_code, 201)

    def test_blocked_stock_rejected_atomically(self):
        _, pending = self.fx.packaged_receipt(count=1, invoice_number='QC')
        inv.putaway_package(pending[0], self.fx.quarantine, self.fx.users['WAREHOUSE'])
        Package.objects.filter(pk=self.mid.pk).update(is_recalled=True)
        resp = self.issue([(self.old, 100), (fresh(pending[0]), 10), (self.mid, 10)], client=self.mgr, override_reason='x')
        self.assertEqual(resp.json()['error'], 'issue_rejected')
        problems = ' '.join(resp.json()['details']['packages'])
        self.assertIn('QC status is Pending Sampling', problems)
        self.assertIn('recalled', problems)
        self.assertEqual(fresh(self.old).current_quantity, D('100'))  # nothing issued
        self.assertFalse(MaterialIssue.objects.exists())

    def test_whole_package_rule(self):
        self.mat.allow_partial_issue = False
        self.mat.save()
        self.assertEqual(self.issue([(self.old, 40)]).json()['error'], 'issue_rejected')
        self.assertEqual(self.issue([(self.old, 100)]).status_code, 201)

    def test_material_must_be_on_request(self):
        req = self.request('10').json()
        _, other = self.fx.packaged_receipt(count=1, material=self.fx.material, invoice_number='OTHER')
        inv.putaway_package(other[0], self.fx.quarantine, self.fx.users['WAREHOUSE'])
        Package.objects.filter(pk=other[0].pk).update(qc_status=QCStatus.PASSED)
        resp = self.issue([(fresh(other[0]), 10)], material_request=req['id'])
        self.assertIn('is not on request', resp.json()['details']['packages'][0])

    def test_retry_safe_issue(self):
        first = self.issue([(self.old, 30)], key='iss-1')
        second = self.issue([(self.old, 30)], key='iss-1')
        self.assertEqual((first.status_code, second.status_code), (201, 200))
        self.assertEqual(first.json()['issue_number'], second.json()['issue_number'])
        self.assertEqual(fresh(self.old).current_quantity, D('70'))

    def test_roles_and_receiver_required(self):
        self.assertEqual(self.issue([(self.old, 10)], client=client_for(self.fx.users['GATE'])).status_code, 403)
        self.assertEqual(self.issue([(self.old, 10)], client=self.fac).status_code, 403)
        resp = self.wh.post(f'{API}/issues/', {'factory': self.factory.pk,
                                               'lines': [{'package': str(self.old.pk), 'quantity': '1'}]}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_factory_acknowledges_receipt(self):
        req = self.request('100').json()
        issue = self.issue([(self.old, 100)], material_request=req['id']).json()
        other = client_for(make_user('FACTORY', username='f2user'))
        self.assertEqual(other.post(f"{API}/issues/{issue['id']}/acknowledge/").status_code, 404)
        self.assertEqual(self.wh.post(f"{API}/issues/{issue['id']}/acknowledge/").status_code, 403)
        resp = self.fac.post(f"{API}/issues/{issue['id']}/acknowledge/")
        self.assertEqual((resp.json()['status'], resp.json()['acknowledged_by']), ('ACKNOWLEDGED', self.fx.users['FACTORY'].pk))
        self.assertEqual(self.fac.post(f"{API}/issues/{issue['id']}/acknowledge/").status_code, 400)

    def test_issue_appears_in_package_timeline(self):
        issue = self.issue([(self.old, 40)]).json()
        events = self.wh.get(f'{API}/packages/{self.old.id}/timeline/').json()
        self.assertIn(f"Issued on {issue['issue_number']}", [e['title'] for e in events])


class ReturnTests(IssueTestCase):
    def setUp(self):
        super().setUp()
        self.issue_body = self.issue([(self.old, 100)]).json()
        self.line = MaterialIssueLine.objects.get()

    def ret(self, qty, **extra):
        return self.wh.post(f'{API}/returns/', {'issue_line': str(self.line.pk), 'returned_quantity': qty,
                                                'reason': 'Batch change at line', **extra}, format='json')

    def test_return_creates_blocked_child_package(self):
        resp = self.ret('30')
        self.assertEqual(resp.status_code, 201, resp.content)
        child = Package.objects.get(package_id=resp.json()['return_package_code'])
        self.assertEqual((child.parent_package, child.current_quantity, child.status),
                         (self.old, D('30'), PackageStatus.CREATED))
        self.assertTrue(child.is_on_hold)  # QC not required → inspection hold
        self.assertFalse(child.is_available)
        self.assertEqual(fresh(self.old).status, PackageStatus.ISSUED)  # original untouched
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_qc_material_returns_need_retest(self):
        _, pkgs = self.fx.packaged_receipt(count=1, material=self.fx.material, invoice_number='QCM')
        inv.putaway_package(pkgs[0], self.fx.quarantine, self.fx.users['WAREHOUSE'])
        Package.objects.filter(pk=pkgs[0].pk).update(qc_status=QCStatus.PASSED)
        self.issue([(fresh(pkgs[0]), 50)])
        line = MaterialIssueLine.objects.get(package=pkgs[0])
        resp = self.wh.post(f'{API}/returns/', {'issue_line': str(line.pk), 'returned_quantity': '10', 'reason': 'unused'},
                            format='json')
        child = Package.objects.get(package_id=resp.json()['return_package_code'])
        self.assertEqual(child.qc_status, QCStatus.RETEST_REQUIRED)
        self.assertFalse(child.is_on_hold)

    def test_cannot_return_more_than_issued(self):
        self.assertEqual(self.ret('60').status_code, 201)
        self.assertEqual(self.ret('41').json()['error'], 'exceeds_issued')
        self.assertEqual(self.ret('40').status_code, 201)

    def test_role_and_reason(self):
        self.assertEqual(self.ret('1', reason='').status_code, 400)
        resp = self.fac.post(f'{API}/returns/', {'issue_line': str(self.line.pk), 'returned_quantity': '1', 'reason': 'x'},
                             format='json')
        self.assertEqual(resp.status_code, 403)


class DisposalTests(IssueTestCase):
    def test_disposal_of_good_stock_needs_manager(self):
        payload = {'package': str(self.old.pk), 'quantity': '20', 'reason': 'Bag torn'}
        self.assertEqual(self.wh.post(f'{API}/disposals/', payload, format='json').status_code, 403)
        resp = self.mgr.post(f'{API}/disposals/', payload, format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.json()['disposal_number'].startswith('DSP-'))
        self.assertEqual(fresh(self.old).current_quantity, D('80'))

    def test_warehouse_scraps_blocked_stock_fully(self):
        Package.objects.filter(pk=self.old.pk).update(qc_status=QCStatus.FAILED)
        resp = self.wh.post(f'{API}/disposals/', {'package': str(self.old.pk), 'quantity': '100', 'reason': 'Rejected lot'},
                            format='json')
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(fresh(self.old).status, PackageStatus.DISPOSED)
        self.assertEqual(fresh(self.old).movements.latest('created_at').reason, 'DISPOSAL')

    def test_supplier_return(self):
        resp = self.mgr.post(f'{API}/disposals/', {'package': str(self.old.pk), 'quantity': '100', 'reason': 'Wrong grade',
                                                   'disposal_type': 'SUPPLIER_RETURN', 'reference': 'DN-12'}, format='json')
        self.assertTrue(resp.json()['disposal_number'].startswith('SRT-'))
        self.assertEqual(fresh(self.old).status, PackageStatus.SUPPLIER_RETURNED)
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_validation(self):
        self.assertEqual(self.mgr.post(f'{API}/disposals/', {'package': str(self.old.pk), 'quantity': '101', 'reason': 'x'},
                                       format='json').status_code, 400)
        self.assertEqual(self.mgr.post(f'{API}/disposals/', {'package': str(self.old.pk), 'quantity': '1', 'reason': ''},
                                       format='json').status_code, 400)


class AdjustmentTests(IssueTestCase):
    def adjust(self, kind, qty, client=None):
        return (client or self.wh).post(f'{API}/adjustments/', {'package': str(self.old.pk), 'adjustment_type': kind,
                                                               'quantity_change': qty, 'unit': self.fx.kg.pk,
                                                               'reason': 'Cycle count'}, format='json')

    def test_count_correction_approved_by_manager(self):
        adj = self.adjust('CORRECTION', '97.5').json()
        self.assertEqual(adj['status'], 'PENDING')
        self.assertEqual(fresh(self.old).current_quantity, D('100'))  # nothing until approved
        resp = self.mgr.post(f"{API}/adjustments/{adj['id']}/approve/", {'notes': 'Verified'}, format='json')
        self.assertEqual((resp.json()['status'], resp.json()['applied_delta']), ('APPROVED', '-2.5000'))
        self.assertEqual(fresh(self.old).current_quantity, D('97.5'))
        self.assertTrue(AuditEvent.objects.filter(action='ADJUST', entity_id=str(self.old.pk)).exists())
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_separation_of_duties_and_rejection(self):
        adj = self.adjust('INCREASE', '5', client=self.mgr)
        self.assertEqual(adj.status_code, 403)  # managers approve, they do not request
        adj = self.adjust('INCREASE', '5', client=client_for(self.fx.users['ADMIN'])).json()
        admin = client_for(self.fx.users['ADMIN'])
        self.assertEqual(admin.post(f"{API}/adjustments/{adj['id']}/approve/", format='json').json()['error'],
                         'separation_of_duties')
        self.assertEqual(self.mgr.post(f"{API}/adjustments/{adj['id']}/reject/", {}, format='json').status_code, 400)
        resp = self.mgr.post(f"{API}/adjustments/{adj['id']}/reject/", {'notes': 'Recount found no excess'}, format='json')
        self.assertEqual(resp.json()['status'], 'REJECTED')
        self.assertEqual(fresh(self.old).current_quantity, D('100'))

    def test_decrease_cannot_go_negative_or_below_reserved(self):
        adj = self.adjust('DECREASE', '150').json()
        self.assertEqual(self.mgr.post(f"{API}/adjustments/{adj['id']}/approve/", format='json').status_code, 400)
        req = self.request('100').json()
        self.wh.post(f"{API}/requests/{req['id']}/allocate/", format='json')
        adj = self.adjust('DECREASE', '10').json()
        self.assertEqual(self.mgr.post(f"{API}/adjustments/{adj['id']}/approve/", format='json').status_code, 400)
