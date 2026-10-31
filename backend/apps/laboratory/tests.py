"""
Phase 5 — laboratory sampling, results, review, disposition, holds, recalls, release tags.
Covers spec test items 7 (sample-to-package association), 8 (pass / fail / hold / retest /
approval permissions) and the Stage 6–7 quality rules.
"""
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.models import AuditEvent, SystemConfiguration
from apps.core.testing import Fixtures, client_for
from apps.inventory import services as inv
from apps.inventory.models import Package, QCStatus, PackageLabelEvent
from apps.laboratory import services as lab
from apps.laboratory.models import LabSample, LabTestResult, SampleStatus, TestRunStatus
from apps.master.models import LabTestDefinition, MaterialTestRequirement
from apps.notifications.models import Notification

API = '/api/v1/laboratory'


def fresh(obj):
    return type(obj).objects.get(pk=obj.pk)


class LabTestCase(TestCase):
    def setUp(self):
        self.fx = Fixtures()
        self.pct = self.fx.kg.__class__.objects.create(code='PCT', name='Percent', symbol='%', unit_type='OTHER')
        self.moist = LabTestDefinition.objects.create(code='MOIST', name='Moisture', result_type='NUMERIC',
                                                      unit=self.pct, max_value=Decimal('0.5'))
        self.purity = LabTestDefinition.objects.create(code='PURITY', name='Purity', result_type='NUMERIC',
                                                       unit=self.pct, min_value=Decimal('98.5'))
        self.appear = LabTestDefinition.objects.create(code='APPEAR', name='Appearance', result_type='TEXT',
                                                       acceptable_text_values=['White powder'], is_required=False)
        for t in (self.moist, self.purity, self.appear):
            MaterialTestRequirement.objects.create(material=self.fx.material, test_definition=t)
        _, self.pkgs = self.fx.packaged_receipt(count=3, each='100')
        for p in self.pkgs:
            inv.putaway_package(p, self.fx.quarantine, self.fx.users['WAREHOUSE'])
        self.pkgs = [fresh(p) for p in self.pkgs]
        self.tech, self.reviewer = self.fx.users['LAB_TECH'], self.fx.users['LAB_REVIEWER']
        self.c_tech, self.c_rev = client_for(self.tech), client_for(self.reviewer)

    # --- helpers -----------------------------------------------------------
    def create_sample(self, packages=None, client=None, **extra):
        payload = {'packages': [str(p.pk) for p in (packages or self.pkgs[:2])], **extra}
        return (client or self.c_tech).post(f'{API}/samples/', payload, format='json')

    def sample_through_testing(self, packages=None):
        sample = LabSample.objects.get(pk=self.create_sample(packages).json()['id'])
        lab.collect_sample(sample, {}, self.tech)
        run = lab.start_testing(sample, self.tech)
        return fresh(sample), run

    def enter(self, run, moist='0.2', purity='99.1', appear=None, user=None):
        entries = [{'test_definition': self.moist, 'numeric_result': Decimal(moist)},
                   {'test_definition': self.purity, 'numeric_result': Decimal(purity)}]
        if appear is not None:
            entries.append({'test_definition': self.appear, 'text_result': appear})
        return lab.record_results(run, entries, user or self.tech)

    def submitted(self, **results):
        sample, run = self.sample_through_testing()
        self.enter(run, **results)
        lab.submit_run(run, self.tech)
        return fresh(sample), fresh(run)

    def decide(self, sample, decision, client=None, **extra):
        return (client or self.c_rev).post(f'{API}/samples/{sample.id}/disposition/',
                                           {'decision': decision, 'reason': 'QC review', **extra}, format='json')

    def qc(self, pkg):
        return fresh(pkg).qc_status


class SamplingTests(LabTestCase):
    def test_sample_covers_explicit_packages_with_default_tests(self):
        resp = self.create_sample()
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body['sample_id'].startswith('SMP-'))
        self.assertEqual(sorted(sp['package_code'] for sp in body['sample_packages']),
                         sorted(p.package_id for p in self.pkgs[:2]))
        self.assertEqual(sorted(t['code'] for t in body['requested_tests_detail']), ['APPEAR', 'MOIST', 'PURITY'])
        self.assertEqual(body['batch_number'], 'LOT-A')
        self.assertFalse(Notification.objects.filter(recipient=self.tech).exists())  # requester is not notified

    def test_package_can_be_in_only_one_active_sample(self):
        self.create_sample()
        resp = self.create_sample([self.pkgs[1], self.pkgs[2]])
        self.assertEqual(resp.status_code, 400)
        self.assertIn('already covered by active sample', resp.json()['details']['packages'][0])

    def test_cancel_frees_packages(self):
        sample = self.create_sample().json()
        self.assertEqual(self.c_tech.post(f"{API}/samples/{sample['id']}/cancel/", {}, format='json').status_code, 400)
        self.assertEqual(self.c_tech.post(f"{API}/samples/{sample['id']}/cancel/", {'reason': 'wrong lot'}, format='json').status_code, 200)
        self.assertEqual(self.create_sample().status_code, 201)

    def test_sample_scope_single_lot(self):
        Package.objects.filter(pk=self.pkgs[2].pk).update(batch_number='LOT-B')
        resp = self.create_sample(self.pkgs)
        self.assertEqual(resp.json()['error'], 'invalid_sample_scope')
        SystemConfiguration.objects.update_or_create(key='lab.sample_scope', defaults={'value': '"PACKAGE"'})
        self.assertEqual(self.create_sample(self.pkgs).status_code, 201)

    def test_cannot_mix_materials_or_sample_ineligible_packages(self):
        _, other = self.fx.packaged_receipt(count=1, material=self.fx.material_noqc, invoice_number='X2')
        resp = self.create_sample([self.pkgs[0], other[0]])
        self.assertIn('different material', ' '.join(resp.json()['details']['packages']))
        resp = self.create_sample([other[0]])  # QC not required
        self.assertIn('cannot be sampled', ' '.join(resp.json()['details']['packages']))
        Package.objects.filter(pk=self.pkgs[0].pk).update(is_recalled=True)
        self.assertIn('recalled', ' '.join(self.create_sample([self.pkgs[0]]).json()['details']['packages']))

    def test_no_tests_configured(self):
        MaterialTestRequirement.objects.all().delete()
        self.assertEqual(self.create_sample().json()['error'], 'no_tests')

    def test_roles(self):
        for role in ('WAREHOUSE', 'RECEIVING', 'FACTORY', 'AUDITOR'):
            self.assertEqual(self.create_sample(client=client_for(self.fx.users[role])).status_code, 403, role)

    def test_collection_draws_through_ledger(self):
        sample = self.create_sample().json()
        resp = self.c_tech.post(f"{API}/samples/{sample['id']}/collect/", {
            'draws': [{'package': str(self.pkgs[0].pk), 'quantity': '0.5'}], 'sample_condition': 'Sealed jar'},
            format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body['status'], body['sampled_quantity'], body['sampled_unit_symbol']),
                         ('COLLECTED', '0.5000', 'kg'))
        self.assertEqual(fresh(self.pkgs[0]).current_quantity, Decimal('99.5'))
        self.assertEqual([self.qc(p) for p in self.pkgs], [QCStatus.SAMPLED, QCStatus.SAMPLED, QCStatus.PENDING_SAMPLING])
        self.assertEqual(fresh(self.pkgs[0]).movements.filter(reason='SAMPLE').count(), 1)
        self.assertEqual(inv.reconciliation_issues(), [])

    def test_collection_validation(self):
        sample = self.create_sample().json()
        url = f"{API}/samples/{sample['id']}/collect/"
        self.assertEqual(self.c_tech.post(url, {'draws': [{'package': str(self.pkgs[2].pk), 'quantity': '1'}]}, format='json').status_code, 400)
        self.assertEqual(self.c_tech.post(url, {'draws': [{'package': str(self.pkgs[0].pk), 'quantity': '101'}]}, format='json').status_code, 400)
        self.assertEqual(fresh(self.pkgs[0]).current_quantity, Decimal('100'))


class ResultTests(LabTestCase):
    def test_testing_requires_collection(self):
        sample = LabSample.objects.get(pk=self.create_sample().json()['id'])
        self.assertEqual(self.c_tech.post(f'{API}/samples/{sample.id}/start-testing/', {}, format='json').status_code, 400)
        lab.collect_sample(sample, {}, self.tech)
        resp = self.c_tech.post(f'{API}/samples/{sample.id}/start-testing/', {}, format='json')
        self.assertEqual((resp.status_code, resp.json()['run_number']), (201, 1))
        self.assertEqual(self.qc(self.pkgs[0]), QCStatus.TESTING)

    def test_numeric_and_text_results_evaluated_server_side(self):
        _, run = self.sample_through_testing()
        resp = self.c_tech.post(f'{API}/test-runs/{run.id}/results/', {'results': [
            {'test_definition': self.moist.pk, 'numeric_result': '0.7'},
            {'test_definition': self.purity.pk, 'numeric_result': '98.5'},
            {'test_definition': self.appear.pk, 'text_result': 'white POWDER'},
        ]}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        by_code = {r['test_code']: r for r in resp.json()}
        self.assertEqual(by_code['MOIST']['result_status'], 'FAIL')
        self.assertEqual(by_code['MOIST']['max_limit'], '0.500000')
        self.assertEqual(by_code['PURITY']['result_status'], 'PASS')  # boundary inclusive
        self.assertEqual(by_code['APPEAR']['result_status'], 'PASS')

    def test_limits_are_snapshotted(self):
        _, run = self.sample_through_testing()
        self.enter(run)
        LabTestDefinition.objects.filter(pk=self.moist.pk).update(max_value=Decimal('0.1'))
        r = LabTestResult.objects.get(test_definition=self.moist)
        self.assertEqual((r.max_limit, r.result_status), (Decimal('0.5'), 'PASS'))

    def test_text_without_criterion_needs_interpretation(self):
        free = LabTestDefinition.objects.create(code='ODOUR', name='Odour', result_type='TEXT', is_required=False)
        sample, run = self.sample_through_testing()
        sample.requested_tests.add(free)
        url = f'{API}/test-runs/{run.id}/results/'
        self.assertEqual(self.c_tech.post(url, {'results': [{'test_definition': free.pk, 'text_result': 'faint'}]}, format='json').status_code, 400)
        resp = self.c_tech.post(url, {'results': [{'test_definition': free.pk, 'text_result': 'faint', 'result_status': 'PASS',
                                                    'interpretation': 'Within normal range'}]}, format='json')
        self.assertEqual(resp.json()[0]['result_status'], 'PASS')

    def test_unrequested_test_and_wrong_unit_rejected(self):
        other = LabTestDefinition.objects.create(code='PH', name='pH', result_type='NUMERIC')
        _, run = self.sample_through_testing()
        url = f'{API}/test-runs/{run.id}/results/'
        self.assertEqual(self.c_tech.post(url, {'results': [{'test_definition': other.pk, 'numeric_result': '7'}]}, format='json').status_code, 400)
        self.assertEqual(self.c_tech.post(url, {'results': [{'test_definition': self.moist.pk, 'numeric_result': '0.1',
                                                             'result_unit': self.fx.kg.pk}]}, format='json').status_code, 400)

    def test_corrections_are_revisions_with_history(self):
        _, run = self.sample_through_testing()
        self.enter(run, moist='0.7')
        self.enter(run, moist='0.3')
        r = LabTestResult.objects.get(test_definition=self.moist)
        self.assertEqual((r.revision, r.result_status), (2, 'PASS'))
        events = AuditEvent.objects.filter(entity_type='LabTestResult', entity_id=str(r.id)).order_by('timestamp')
        self.assertEqual([e.action for e in events], ['CREATE', 'UPDATE'])
        self.assertEqual(events[1].before_state['status'], 'FAIL')

    def test_submit_requires_required_results_then_locks(self):
        _, run = self.sample_through_testing()
        lab.record_results(run, [{'test_definition': self.moist, 'numeric_result': Decimal('0.1')}], self.tech)
        resp = self.c_tech.post(f'{API}/test-runs/{run.id}/submit/', format='json')
        self.assertEqual(resp.json()['details']['missing_tests'], ['PURITY'])  # APPEAR is optional
        self.enter(run)
        resp = self.c_tech.post(f'{API}/test-runs/{run.id}/submit/', format='json')
        self.assertEqual((resp.json()['status'], resp.json()['outcome']), ('SUBMITTED', 'PASS'))
        self.assertEqual(fresh(run.sample).status, SampleStatus.RESULTS_SUBMITTED)
        resp = self.c_tech.post(f'{API}/test-runs/{run.id}/results/', {'results': [
            {'test_definition': self.moist.pk, 'numeric_result': '0.9'}]}, format='json')
        self.assertEqual(resp.json()['error'], 'run_locked')
        self.assertTrue(Notification.objects.filter(recipient=self.reviewer, entity_type='LabSample').exists())

    def test_only_lab_roles_enter_results(self):
        _, run = self.sample_through_testing()
        resp = client_for(self.fx.users['WAREHOUSE']).post(f'{API}/test-runs/{run.id}/results/', {'results': [
            {'test_definition': self.moist.pk, 'numeric_result': '0.1'}]}, format='json')
        self.assertEqual(resp.status_code, 403)


class DispositionTests(LabTestCase):
    def test_release_makes_covered_packages_available_only(self):
        sample, run = self.submitted()
        resp = self.decide(sample, 'RELEASED')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(sorted(resp.json()['released_package_ids']), sorted(p.package_id for p in self.pkgs[:2]))
        for p in self.pkgs[:2]:
            self.assertEqual(self.qc(p), QCStatus.PASSED)
            self.assertTrue(fresh(p).is_available)
        # Same lot, not covered by the sample: never approved implicitly
        self.assertEqual(self.qc(self.pkgs[2]), QCStatus.PENDING_SAMPLING)
        self.assertEqual(fresh(sample).status, SampleStatus.COMPLETED)
        self.assertEqual((fresh(run).status, fresh(run).reviewed_by), (TestRunStatus.REVIEWED, self.reviewer))

    def test_failing_results_cannot_be_released_but_can_be_rejected(self):
        sample, _ = self.submitted(moist='0.9')
        resp = self.decide(sample, 'RELEASED')
        self.assertEqual(resp.json()['error'], 'results_not_passing')
        self.assertEqual(self.decide(sample, 'REJECTED').status_code, 201)
        self.assertEqual(self.qc(self.pkgs[0]), QCStatus.FAILED)
        self.assertFalse(fresh(self.pkgs[0]).is_available)
        self.assertTrue(Notification.objects.filter(recipient=self.fx.users['WAREHOUSE'], level='ERROR').exists())

    def test_failed_optional_test_also_blocks_release(self):
        sample, _ = self.submitted(appear='Yellow lumps')
        self.assertEqual(self.decide(sample, 'RELEASED').json()['error'], 'results_not_passing')

    def test_lab_tech_cannot_decide(self):
        sample, _ = self.submitted()
        self.assertEqual(self.decide(sample, 'RELEASED', client=self.c_tech).status_code, 403)
        self.assertEqual(self.decide(sample, 'RELEASED', client=client_for(self.fx.users['WAREHOUSE'])).status_code, 403)

    def test_separation_of_duties(self):
        sample, run = self.sample_through_testing()
        self.enter(run, user=self.reviewer)  # reviewer entered the results
        lab.submit_run(run, self.tech)
        resp = self.decide(sample, 'RELEASED')
        self.assertEqual((resp.status_code, resp.json()['error']), (403, 'separation_of_duties'))
        SystemConfiguration.objects.update_or_create(key='lab.separation_of_duties', defaults={'value': 'false'})
        self.assertEqual(self.decide(sample, 'RELEASED').status_code, 201)

    def test_decision_needs_submitted_results(self):
        sample, _ = self.sample_through_testing()
        self.assertEqual(self.decide(sample, 'REJECTED').json()['error'], 'results_not_submitted')

    def test_reason_required(self):
        sample, _ = self.submitted()
        resp = self.c_rev.post(f'{API}/samples/{sample.id}/disposition/', {'decision': 'RELEASED'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_retest_creates_new_run_and_keeps_failed_history(self):
        sample, run1 = self.submitted(moist='0.9')
        self.assertEqual(self.decide(sample, 'RETEST').status_code, 201)
        self.assertEqual((fresh(sample).status, self.qc(self.pkgs[0])), (SampleStatus.RETEST_REQUIRED, QCStatus.RETEST_REQUIRED))
        run2 = lab.start_testing(fresh(sample), self.tech)
        self.assertEqual(run2.run_number, 2)
        self.enter(run2, moist='0.3')
        lab.submit_run(run2, self.tech)
        self.assertEqual(self.decide(sample, 'RELEASED').status_code, 201)
        run1 = fresh(run1)
        self.assertFalse(run1.is_current)
        self.assertEqual(run1.results.get(test_definition=self.moist).result_status, 'FAIL')  # never deleted
        detail = self.c_rev.get(f'{API}/samples/{sample.id}/').json()
        self.assertEqual([d['decision'] for d in detail['dispositions']], ['RELEASED', 'RETEST'])
        self.assertEqual([r['run_number'] for r in detail['test_runs']], [2, 1])

    def test_hold_then_final_decision(self):
        sample, _ = self.submitted()
        self.assertEqual(self.decide(sample, 'ON_HOLD').status_code, 201)
        self.assertEqual((fresh(sample).status, self.qc(self.pkgs[0])), (SampleStatus.RESULTS_SUBMITTED, QCStatus.ON_HOLD))
        self.assertEqual(self.decide(sample, 'RELEASED').status_code, 201)
        self.assertEqual(self.qc(self.pkgs[0]), QCStatus.PASSED)
        self.assertEqual(self.decide(sample, 'REJECTED').json()['error'], 'results_not_submitted')  # completed

    def test_partial_acceptance(self):
        sample, _ = self.submitted()
        bad = self.decide(sample, 'PARTIAL', released_packages=[str(p.pk) for p in self.pkgs[:2]])
        self.assertEqual(bad.status_code, 400)  # all packages ≠ partial
        bad = self.decide(sample, 'PARTIAL', released_packages=[str(self.pkgs[2].pk)])
        self.assertEqual(bad.status_code, 400)  # not in sample
        resp = self.decide(sample, 'PARTIAL', released_packages=[str(self.pkgs[0].pk)], remainder_decision='ON_HOLD')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual((self.qc(self.pkgs[0]), self.qc(self.pkgs[1])), (QCStatus.PASSED, QCStatus.ON_HOLD))
        self.assertEqual(resp.json()['released_package_ids'], [self.pkgs[0].package_id])

    def test_split_children_follow_their_parent_decision(self):
        sample, _ = self.submitted()
        _, children = inv.split_package(self.pkgs[0], [Decimal('40')], self.fx.users['WAREHOUSE'], reason='repack')
        self.decide(sample, 'REJECTED')
        self.assertEqual(self.qc(children[0]), QCStatus.FAILED)
        self.assertEqual(self.qc(self.pkgs[2]), QCStatus.PENDING_SAMPLING)

    def test_periodic_retest_extends_retest_date(self):
        sample, _ = self.submitted()
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertEqual(self.decide(sample, 'RELEASED', new_retest_date=past).status_code, 400)
        future = timezone.localdate() + timedelta(days=180)
        self.assertEqual(self.decide(sample, 'RELEASED', new_retest_date=future.isoformat()).status_code, 201)
        self.assertEqual(fresh(self.pkgs[0]).retest_date, future)

    def test_package_and_sample_timelines(self):
        sample, _ = self.submitted()
        self.decide(sample, 'RELEASED')
        kinds = [e['title'] for e in self.c_rev.get(f'/api/v1/inventory/packages/{self.pkgs[0].id}/timeline/').json()
                 if e['kind'] == 'LAB']
        self.assertTrue(any('Test result' in k for k in kinds))
        self.assertTrue(any('Quality disposition' in k for k in kinds))
        entity_types = {e['entity_type'] for e in self.c_rev.get(f'{API}/samples/{sample.id}/timeline/').json()}
        self.assertEqual(entity_types, {'LabSample', 'LabTestResult', 'QualityDisposition'})

    def test_released_stock_can_be_issued_end_to_end(self):
        sample, _ = self.submitted()
        self.decide(sample, 'RELEASED')
        suggestion = client_for(self.fx.users['WAREHOUSE']).get(
            '/api/v1/inventory/stock/fifo-suggestion/', {'material_id': self.fx.material.pk, 'quantity': '150'}).json()
        self.assertEqual(sorted(s['package_id'] for s in suggestion['suggestions']),
                         sorted(p.package_id for p in self.pkgs[:2]))
        self.assertEqual(Decimal(suggestion['shortfall']), 0)


class HoldAndRecallTests(LabTestCase):
    def setUp(self):
        super().setUp()
        sample, _ = self.submitted()
        lab.decide(sample, {'decision': 'RELEASED', 'reason': 'ok'}, self.reviewer)

    def hold(self, client=None, **data):
        return (client or self.c_rev).post(f'{API}/holds/', {'reason': 'Customer complaint', **data}, format='json')

    def test_hold_blocks_released_stock_until_released(self):
        resp = self.hold(packages=[str(self.pkgs[0].pk)])
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.json()['reference'].startswith('HLD-'))
        self.assertFalse(fresh(self.pkgs[0]).is_available)
        self.assertEqual(self.qc(self.pkgs[0]), QCStatus.PASSED)  # quality status untouched
        hid = resp.json()['id']
        self.assertEqual(self.c_rev.post(f'{API}/holds/{hid}/release/', {}, format='json').status_code, 400)
        self.assertEqual(self.c_rev.post(f'{API}/holds/{hid}/release/', {'notes': 'Investigated'}, format='json').status_code, 200)
        self.assertTrue(fresh(self.pkgs[0]).is_available)

    def test_overlapping_holds(self):
        h1 = self.hold(packages=[str(self.pkgs[0].pk)]).json()['id']
        self.hold(packages=[str(self.pkgs[0].pk)])
        self.c_rev.post(f'{API}/holds/{h1}/release/', {'notes': 'done'}, format='json')
        self.assertTrue(fresh(self.pkgs[0]).is_on_hold)

    def test_lot_recall_includes_issued_packages_and_reports_impact(self):
        from apps.master.models import Factory
        from apps.inventory import issuing
        issue = issuing.create_issue({'factory': Factory.objects.create(code='F1', name='Factory 1'),
                                      'received_by_name': 'Line 1',
                                      'lines': [{'package': self.pkgs[0], 'quantity': Decimal('100')}]},
                                     self.fx.users['WAREHOUSE'])
        resp = self.hold(hold_type='RECALL', material=self.fx.material.pk, batch_number='lot-a')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(len(resp.json()['package_ids']), 3)
        self.assertTrue(all(fresh(p).is_recalled for p in self.pkgs))
        impact = self.c_rev.get(f"{API}/holds/{resp.json()['id']}/impact/").json()
        self.assertEqual([(i['issue_number'], i['factory']) for i in impact['issued']], [(issue.issue_number, 'Factory 1')])
        self.assertEqual(len(impact['in_stock']), 2)

    def test_roles_and_validation(self):
        self.assertEqual(self.hold(client=client_for(self.fx.users['WAREHOUSE']), packages=[str(self.pkgs[0].pk)]).status_code, 403)
        self.assertEqual(self.hold().status_code, 400)  # nothing selected
        self.assertEqual(self.c_rev.post(f'{API}/holds/', {'packages': [str(self.pkgs[0].pk)]}, format='json').status_code, 400)


class ReleaseTagTests(LabTestCase):
    def tag(self, pkg, **data):
        return client_for(self.fx.users['WAREHOUSE']).post(f'/api/v1/inventory/packages/{pkg.id}/print-release-tag/', data, format='json')

    def test_tag_only_when_server_confirms_release(self):
        resp = self.tag(self.pkgs[0])
        self.assertEqual(resp.json()['error'], 'not_released')
        sample, _ = self.submitted()
        lab.decide(sample, {'decision': 'RELEASED', 'reason': 'ok'}, self.reviewer)
        self.assertEqual(self.tag(self.pkgs[0]).status_code, 200)
        self.assertEqual(self.tag(self.pkgs[0]).status_code, 400)  # reprint needs reason
        self.assertEqual(self.tag(self.pkgs[0], reason='Tag damaged').status_code, 200)
        self.assertEqual(PackageLabelEvent.objects.filter(package=self.pkgs[0], event_type='RELEASE_TAG').count(), 2)
        lab.create_hold({'packages': [self.pkgs[0]], 'reason': 'complaint'}, self.reviewer)
        self.assertEqual(self.tag(self.pkgs[0], reason='x').json()['error'], 'not_released')


class DashboardAndAttachmentTests(LabTestCase):
    def test_dashboard_counts(self):
        self.submitted()
        LabSample.objects.get(pk=self.create_sample([self.pkgs[2]]).json()['id'])
        d = self.c_tech.get(f'{API}/samples/dashboard/').json()
        self.assertEqual((d['awaiting_collection'], d['awaiting_review'], d['packages_awaiting_sampling']), (1, 1, 0))
        pending = self.c_tech.get(f'{API}/samples/pending-packages/').json()
        self.assertEqual(pending['count'], 0)

    def test_overdue_filter(self):
        self.create_sample(required_by=(timezone.now() - timedelta(hours=1)).isoformat())
        self.assertEqual(self.c_tech.get(f'{API}/samples/', {'overdue': 'true'}).json()['count'], 1)
        self.assertEqual(self.c_tech.get(f'{API}/samples/dashboard/').json()['overdue'], 1)

    def test_report_upload_and_protected_download(self):
        media = tempfile.mkdtemp()
        try:
            with override_settings(MEDIA_ROOT=media):
                sample = self.create_sample().json()
                resp = self.c_tech.post(f"{API}/samples/{sample['id']}/attachments/", {
                    'file': SimpleUploadedFile('coa.pdf', b'%PDF-1.7 coa'), 'document_type': 'COA'}, format='multipart')
                self.assertEqual(resp.status_code, 201, resp.content)
                url = resp.json()['download_url']
                self.assertEqual(client_for(self.fx.users['AUDITOR']).get(url).status_code, 200)
                self.assertEqual(client_for(self.fx.users['FACTORY']).get(url).status_code, 403)
                bad = self.c_tech.post(f"{API}/samples/{sample['id']}/attachments/", {
                    'file': SimpleUploadedFile('coa.pdf', b'not a pdf'), 'document_type': 'COA'}, format='multipart')
                self.assertEqual(bad.status_code, 400)
        finally:
            shutil.rmtree(media, ignore_errors=True)
