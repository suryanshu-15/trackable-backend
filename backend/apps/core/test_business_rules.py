"""
The business answers recorded in docs/DECISIONS.md, tested under the shipped configuration
defaults (Fixtures(policy={}) — no generic overrides): bags counted in tonnes with expiry and
retest dates, whole-line acceptance, one sample per bag, optional tests not blocking release,
expiry extension after retest, whole-bag issue against a material request, and retention.
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from apps.core.exceptions import BusinessRuleViolation
from apps.core.models import AuditEvent
from apps.core.testing import Fixtures, client_for
from apps.inventory import issuing, services as inv
from apps.inventory.models import Package
from apps.laboratory import services as lab
from apps.laboratory.models import LabSample, LabTestResult
from apps.master.models import LabTestDefinition, Material, MaterialTestRequirement
from apps.receiving import services as rcv
from apps.receiving.models import DiscrepancyStatus, VerificationStatus


class BusinessRulesBase(TestCase):
    def setUp(self):
        self.fx = Fixtures(policy={})
        self.u = self.fx.users
        # Model defaults now encode the rules: expiry + retest required, whole packages only.
        self.bagged = Material.objects.create(code='BAG-MAT', name='Bagged material', default_unit=self.fx.tonne)
        self.required = LabTestDefinition.objects.create(code='REQ', name='Required test', result_type='NUMERIC',
                                                         max_value=Decimal('1'), is_required=True)
        self.optional = LabTestDefinition.objects.create(code='OPT', name='Optional test', result_type='BOOLEAN',
                                                         is_required=False)
        for t in (self.required, self.optional):
            MaterialTestRequirement.objects.create(material=self.bagged, test_definition=t)
        today = timezone.localdate()
        self.dates = {'expiry_date': today + timedelta(days=300), 'retest_date': today + timedelta(days=120)}

    def receipt(self, tonnes='2'):
        return self.fx.receipt_in_unloading(lines=[{
            'material': self.bagged, 'declared_quantity': Decimal(tonnes), 'declared_unit': self.fx.tonne, **self.dates}])

    def bags(self, count=2, each='1'):
        receipt = self.receipt(str(Decimal(each) * count))
        line = receipt.invoice_lines.get()
        self.fx.verify(line, received=str(Decimal(each) * count), unit=self.fx.tonne)
        pkgs = inv.create_packages(line, [{'quantity': Decimal(each)}] * count, self.u['RECEIVING'])
        for p in pkgs:
            inv.record_label_print(p, self.u['RECEIVING'])
        for name in ('mark_verification_pending', 'mark_unloaded'):
            receipt = rcv.transition_receipt(receipt, name, self.u['RECEIVING'])
        for p in pkgs:
            inv.putaway_package(p, self.fx.quarantine, self.u['WAREHOUSE'])
        return [Package.objects.get(pk=p.pk) for p in pkgs]

    def run_tests_on(self, bag, required_value='0.5', optional_pass=True):
        sample = lab.create_sample({'packages': [bag]}, self.u['LAB_TECH'])
        lab.collect_sample(sample, {}, self.u['LAB_TECH'])
        run = lab.start_testing(sample, self.u['LAB_TECH'])
        lab.record_results(run, [{'test_definition': self.required, 'numeric_result': Decimal(required_value)},
                                 {'test_definition': self.optional, 'boolean_result': optional_pass}], self.u['LAB_TECH'])
        lab.submit_run(run, self.u['LAB_TECH'])
        return LabSample.objects.get(pk=sample.pk)

    def release(self, bag, **extra):
        sample = self.run_tests_on(bag)
        lab.decide(sample, {'decision': 'RELEASED', 'reason': 'ok', **extra}, self.u['LAB_REVIEWER'])
        return Package.objects.get(pk=bag.pk)


class PolicyEndpointTests(BusinessRulesBase):
    def test_ui_can_read_the_business_rules(self):
        body = client_for(self.u['FACTORY']).get('/api/v1/policy/').json()
        self.assertEqual(body['package.unit_codes'], ['T'])
        self.assertEqual(body['package.type'], 'BAG')
        self.assertEqual(body['lab.sample_scope'], 'SINGLE')
        self.assertIs(body['issue.whole_packages_only'], True)
        self.assertEqual(self.client.get('/api/v1/policy/').status_code, 401)


class ReceivingRules(BusinessRulesBase):
    def test_line_is_accepted_whole_or_rejected(self):
        line = self.receipt().invoice_lines.get()
        with self.assertRaises(BusinessRuleViolation) as e:
            self.fx.verify(line, received='2', unit=self.fx.tonne, accepted_quantity=Decimal('1.5'))
        self.assertEqual(e.exception.code, 'partial_acceptance_not_allowed')
        with self.assertRaises(BusinessRuleViolation):
            self.fx.verify(line, received='2', unit=self.fx.tonne, damaged_quantity=Decimal('0.5'))
        v = self.fx.verify(line, received='2', unit=self.fx.tonne)
        self.assertEqual(v.verification_status, VerificationStatus.VERIFIED)

    def test_rejected_line_needs_manager_acceptance_and_gets_no_packages(self):
        receipt = self.receipt()
        line = receipt.invoice_lines.get()
        v = self.fx.verify(line, received='2', unit=self.fx.tonne, accepted_quantity=Decimal('0'),
                           damaged_quantity=Decimal('2'), discrepancy_notes='Wet bags')
        self.assertEqual(v.verification_status, VerificationStatus.DISCREPANT)
        self.assertEqual(v.discrepancy_status, DiscrepancyStatus.MULTIPLE)
        rcv.accept_discrepancy(v, self.u['MANAGER'], reason='Rejected, returned on the same truck')
        with self.assertRaises(BusinessRuleViolation):
            inv.create_packages(line, [{'quantity': Decimal('1')}], self.u['RECEIVING'])
        receipt = rcv.transition_receipt(receipt, 'mark_verification_pending', self.u['RECEIVING'])
        receipt = rcv.transition_receipt(receipt, 'mark_unloaded', self.u['RECEIVING'])
        self.assertEqual(receipt.status, 'UNLOADED')

    def test_bags_are_counted_in_tonnes(self):
        line = self.receipt().invoice_lines.get()
        with self.assertRaises(BusinessRuleViolation) as e:
            self.fx.verify(line, received='2000', unit=self.fx.kg)
        self.assertEqual(e.exception.code, 'package_unit_not_allowed')
        self.fx.verify(line, received='2', unit=self.fx.tonne)
        pkg = inv.create_packages(line, [{'quantity': Decimal('1')}], self.u['RECEIVING'])[0]
        self.assertEqual(pkg.unit, self.fx.tonne)
        self.assertEqual(inv.label_data(pkg)['package_type'], 'BAG')

    def test_bags_need_expiry_and_retest_dates(self):
        receipt = self.fx.receipt_in_unloading(lines=[{
            'material': self.bagged, 'declared_quantity': Decimal('1'), 'declared_unit': self.fx.tonne,
            'expiry_date': self.dates['expiry_date']}])
        line = receipt.invoice_lines.get()
        self.fx.verify(line, received='1', unit=self.fx.tonne)
        with self.assertRaisesMessage(BusinessRuleViolation, 'requires a retest date'):
            inv.create_packages(line, [{'quantity': Decimal('1')}], self.u['RECEIVING'])


class LabRules(BusinessRulesBase):
    def test_one_sample_per_bag(self):
        bags = self.bags()
        with self.assertRaises(BusinessRuleViolation) as e:
            lab.create_sample({'packages': bags}, self.u['LAB_TECH'])
        self.assertIn('One sample covers one package', ' '.join(e.exception.details['packages']))
        released = self.release(bags[0])
        self.assertEqual(released.qc_status, 'PASSED')
        self.assertNotEqual(Package.objects.get(pk=bags[1].pk).qc_status, 'PASSED')  # only the sampled bag

    def test_failed_optional_test_does_not_block_release_but_required_does(self):
        bag, other = self.bags()
        sample = self.run_tests_on(bag, optional_pass=False)
        lab.decide(sample, {'decision': 'RELEASED', 'reason': 'optional only'}, self.u['LAB_REVIEWER'])
        self.assertEqual(Package.objects.get(pk=bag.pk).qc_status, 'PASSED')
        sample = self.run_tests_on(other, required_value='5')
        with self.assertRaises(BusinessRuleViolation) as e:
            lab.decide(sample, {'decision': 'RELEASED', 'reason': 'x'}, self.u['LAB_REVIEWER'])
        self.assertEqual(e.exception.code, 'results_not_passing')

    def test_expired_bag_can_be_released_again_with_a_new_expiry_after_retest(self):
        bag = self.release(self.bags(count=1)[0])
        past = timezone.localdate() - timedelta(days=1)
        Package.objects.filter(pk=bag.pk).update(expiry_date=past)
        self.assertIn(f'expired on {past}', Package.objects.get(pk=bag.pk).blocking_reasons())

        sample = self.run_tests_on(Package.objects.get(pk=bag.pk))
        with self.assertRaises(BusinessRuleViolation) as e:
            lab.decide(sample, {'decision': 'RELEASED', 'reason': 'retested'}, self.u['LAB_REVIEWER'])
        self.assertEqual(e.exception.code, 'new_expiry_required')
        new_expiry = timezone.localdate() + timedelta(days=90)
        lab.decide(sample, {'decision': 'RELEASED', 'reason': 'retested', 'new_expiry_date': new_expiry},
                   self.u['LAB_REVIEWER'])
        bag = Package.objects.get(pk=bag.pk)
        self.assertEqual(bag.expiry_date, new_expiry)
        self.assertEqual(sample.dispositions.get(decision='RELEASED').new_expiry_date, new_expiry)
        self.assertTrue(AuditEvent.objects.filter(entity_id=str(bag.pk), extra__kind='DATES_EXTENDED').exists())


class IssueRules(BusinessRulesBase):
    def setUp(self):
        super().setUp()
        self.stock = [self.release(b) for b in self.bags(count=3)]
        for b in self.stock:
            inv.transfer_package(b, self.fx.shelf1, self.u['WAREHOUSE'])
        self.req = issuing.create_request({'factory': self.factory(), 'lines': [
            {'material': self.bagged, 'quantity': Decimal('2.5'), 'unit': self.fx.tonne}]}, self.u['FACTORY'])

    def factory(self):
        from apps.master.models import Factory
        return Factory.objects.get_or_create(code='F1', defaults={'name': 'Factory'})[0]

    def test_only_whole_bags_are_suggested_and_issued(self):
        sugg = issuing.suggest(self.bagged, self.fx.tonne, Decimal('2.5'), request_line=self.req.lines.get())
        self.assertEqual([s['quantity'] for s in sugg['suggestions']], ['1.0000', '1.0000'])
        bag = Package.objects.get(pk=sugg['suggestions'][0]['package'])
        with self.assertRaises(BusinessRuleViolation) as e:
            issuing.create_issue({'material_request': self.req, 'received_by_name': 'Lead',
                                  'lines': [{'package': bag, 'quantity': Decimal('0.5')}]}, self.u['WAREHOUSE'])
        self.assertIn('only whole packages', ' '.join(e.exception.details['packages']))
        issue = issuing.create_issue({'material_request': self.req, 'received_by_name': 'Lead',
                                      'lines': [{'package': bag, 'quantity': Decimal('1')}]}, self.u['WAREHOUSE'])
        self.assertEqual(issue.issue_lines.get().issued_quantity, Decimal('1'))

    def test_issue_needs_a_material_request(self):
        with self.assertRaises(BusinessRuleViolation) as e:
            issuing.create_issue({'factory': self.factory(), 'received_by_name': 'Lead',
                                  'lines': [{'package': self.stock[0], 'quantity': Decimal('1')}]}, self.u['WAREHOUSE'])
        self.assertEqual(e.exception.code, 'request_required')


class RetentionTests(BusinessRulesBase):
    def run_cmd(self, *args):
        out = StringIO()
        call_command('apply_retention', *args, stdout=out)
        return out.getvalue()

    def test_dry_run_then_apply_with_agreed_periods(self):
        bag = self.release(self.bags(count=1)[0])
        old_sample = LabSample.objects.get(sample_packages__package=bag)
        two_years, four_years = timezone.now() - timedelta(days=730), timezone.now() - timedelta(days=4 * 365)
        AuditEvent.objects.filter(pk__in=list(AuditEvent.objects.values_list('pk', flat=True)[:3])).update(timestamp=two_years)
        LabSample.objects.filter(pk=old_sample.pk).update(updated_at=two_years)
        before = AuditEvent.objects.count()

        out = self.run_cmd()
        self.assertIn('audit_events: 3 past retention', out)
        self.assertIn('lab_samples: 0 past retention', out)  # 2 years < 3-year lab retention
        self.assertEqual(AuditEvent.objects.count(), before)

        LabSample.objects.filter(pk=old_sample.pk).update(updated_at=four_years)
        self.run_cmd('--apply')
        self.assertFalse(LabSample.objects.filter(pk=old_sample.pk).exists())
        self.assertFalse(LabTestResult.objects.filter(test_run__sample_id=old_sample.pk).exists())
        self.assertEqual(Package.objects.get(pk=bag.pk).qc_status, 'PASSED')  # stock keeps its status
        purge = AuditEvent.objects.get(entity_type='Retention')
        self.assertEqual(purge.after_state['audit_events'], '3')
        self.assertEqual(AuditEvent.objects.filter(timestamp__lt=timezone.now() - timedelta(days=365)).count(), 0)


class DjangoAdminAccessTests(TestCase):
    def test_admin_role_maintains_master_data_but_cannot_edit_audit(self):
        fx = Fixtures()
        admin, gate = fx.users['ADMIN'], fx.users['GATE']
        admin.refresh_from_db()
        self.assertTrue(admin.is_staff)
        self.client.force_login(admin)
        self.assertEqual(self.client.get('/admin/master/material/').status_code, 200)
        self.assertEqual(self.client.get('/admin/core/systemconfiguration/').status_code, 200)
        self.assertEqual(self.client.get('/admin/core/auditevent/add/').status_code, 403)
        self.client.force_login(gate)
        self.assertNotEqual(self.client.get('/admin/master/material/').status_code, 200)
