"""
Concurrency — real parallel transactions against PostgreSQL (spec test items 4 and 11).
"""
import threading
from decimal import Decimal

from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation
from apps.core.idempotency import run_idempotent
from apps.core.testing import Fixtures
from apps.inventory import services as inv
from apps.inventory.models import Package, QCStatus, InventoryMovement


def run_parallel(fn, n):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(fn())
        except Exception as exc:  # collected and asserted by the test
            errors.append(exc)
        finally:
            connection.close()
            close_old_connections()

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


class ConcurrentPackageCreationTests(TransactionTestCase):
    def test_parallel_retries_with_same_key_create_once(self):
        fx = Fixtures()
        line = fx.receipt_in_unloading().invoice_lines.get()
        fx.verify(line)
        user = fx.users['RECEIVING']
        payload = {'count': 2, 'quantity_each': '50'}

        def create():
            return run_idempotent(user, 'retry-key', f'create_packages:{line.pk}', payload,
                                  lambda: [p.package_id for p in inv.create_packages(
                                      line, [{'quantity': Decimal('50')}] * 2, user)])

        results, errors = run_parallel(create, 4)
        self.assertEqual(errors, [])
        self.assertEqual(Package.objects.count(), 2)
        self.assertEqual(len({tuple(r[0]) for r in results}), 1)  # every caller got the same packages
        self.assertEqual(sum(1 for r in results if not r[1]), 1)  # exactly one did the work

    def test_parallel_creation_without_key_cannot_exceed_accepted(self):
        fx = Fixtures()
        line = fx.receipt_in_unloading().invoice_lines.get()
        fx.verify(line)  # 200 accepted
        user = fx.users['RECEIVING']
        results, errors = run_parallel(lambda: inv.create_packages(line, [{'quantity': Decimal('150')}], user), 3)
        self.assertEqual(len(results), 1)
        self.assertTrue(all(isinstance(e, BusinessRuleViolation) for e in errors))
        self.assertEqual(Package.objects.count(), 1)


class ConcurrentIssueTests(TransactionTestCase):
    def test_same_stock_cannot_be_issued_twice(self):
        fx = Fixtures()
        _, pkgs = fx.packaged_receipt(count=1, each='100', material=fx.material_noqc)
        inv.putaway_package(pkgs[0], fx.shelf1, fx.users['WAREHOUSE'])
        Package.objects.filter(pk=pkgs[0].pk).update(qc_status=QCStatus.NOT_REQUIRED)
        from apps.master.models import Factory
        from apps.inventory import issuing
        factory = Factory.objects.create(code='F', name='F')
        pkg = Package.objects.get(pk=pkgs[0].pk)

        def issue():
            return issuing.create_issue({'factory': factory, 'received_by_name': 'Line 1',
                                         'lines': [{'package': pkg, 'quantity': Decimal('70')}]}, fx.users['WAREHOUSE'])

        results, errors = run_parallel(issue, 4)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 3)
        pkg = Package.objects.get(pk=pkgs[0].pk)
        self.assertEqual(pkg.current_quantity, Decimal('30'))
        self.assertEqual(InventoryMovement.objects.filter(package=pkg, reason='ISSUE').count(), 1)
        self.assertEqual(inv.reconciliation_issues(), [])


class ConcurrentAllocationTests(TransactionTestCase):
    def test_parallel_allocations_never_over_reserve(self):
        from apps.inventory import issuing
        from apps.inventory.models import MaterialRequest, StockReservation
        from apps.master.models import Factory
        from django.db.models import Sum
        fx = Fixtures()
        _, pkgs = fx.packaged_receipt(count=2, each='100', material=fx.material_noqc)
        for p in pkgs:
            inv.putaway_package(p, fx.shelf1, fx.users['WAREHOUSE'])
        factory = Factory.objects.create(code='F', name='F')
        reqs = [issuing.create_request({'factory': factory, 'lines': [
            {'material': fx.material_noqc, 'quantity': Decimal('150'), 'unit': fx.kg}]}, fx.users['FACTORY'])
            for _ in range(3)]
        it = iter(reqs)
        lock = threading.Lock()

        def allocate():
            with lock:
                req = next(it)
            return issuing.allocate_request(MaterialRequest.objects.get(pk=req.pk), fx.users['WAREHOUSE'])

        results, errors = run_parallel(allocate, 3)
        self.assertEqual(errors, [])
        for p in pkgs:
            reserved = StockReservation.objects.filter(package=p, is_active=True).aggregate(q=Sum('reserved_quantity'))['q'] or 0
            self.assertLessEqual(reserved, Decimal('100'))
        total = StockReservation.objects.filter(is_active=True).aggregate(q=Sum('reserved_quantity'))['q']
        self.assertEqual(total, Decimal('200'))  # all stock reserved exactly once
