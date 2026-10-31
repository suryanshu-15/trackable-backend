"""
Load clearly-marked, NON-PRODUCTION demo data. Idempotent.
Every record is prefixed DEMO / [DEMO] so it can never be mistaken for real data.
"""
import os
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.master.models import (
    Supplier, UnitOfMeasure, UoMConversion, MaterialCategory, Material, MaterialTestRequirement,
    Warehouse, StorageLocation, LabTestDefinition, Factory, Department,
)
from apps.users.models import User, Role

UNITS = [
    ('KG', 'Kilogram', 'kg', 'WEIGHT'),
    ('T', 'Tonne', 't', 'WEIGHT'),
    ('L', 'Litre', 'L', 'VOLUME'),
    ('PCS', 'Pieces', 'pcs', 'COUNT'),
    ('BAG', 'Bag', 'bag', 'COUNT'),
    ('PCT', 'Percent', '%', 'OTHER'),
]


class Command(BaseCommand):
    help = 'Load clearly-marked DEMO (non-production) data.'

    def add_arguments(self, parser):
        parser.add_argument('--allow-non-debug', action='store_true',
                            help='Permit running when DEBUG=False (e.g. a staging demo).')
        parser.add_argument('--with-sample-flow', action='store_true',
                            help='Also create DEMO receipts at different workflow stages (via the real services).')

    @transaction.atomic
    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts['allow_non_debug']:
            raise CommandError('Refusing to load demo data with DEBUG=False. Pass --allow-non-debug to override.')
        password = os.environ.get('DEMO_USER_PASSWORD', 'demo-pass-123!')

        units = {}
        for code, name, symbol, kind in UNITS:
            units[code], _ = UnitOfMeasure.objects.get_or_create(
                code=code, defaults={'name': name, 'symbol': symbol, 'unit_type': kind})
        UoMConversion.objects.get_or_create(from_unit=units['T'], to_unit=units['KG'], defaults={'factor': Decimal('1000')})
        UoMConversion.objects.get_or_create(from_unit=units['KG'], to_unit=units['T'], defaults={'factor': Decimal('0.001')})

        suppliers = [
            Supplier.objects.get_or_create(code=f'DEMO-SUP-{i}', defaults={'name': f'[DEMO] Supplier {name}'})[0]
            for i, name in enumerate(['Alpha Chemicals', 'Beta Polymers', 'Gamma Metals'], start=1)
        ]

        raw, _ = MaterialCategory.objects.get_or_create(code='DEMO-RAW', defaults={'name': '[DEMO] Raw material'})
        chem, _ = MaterialCategory.objects.get_or_create(
            code='DEMO-CHEM', defaults={'name': '[DEMO] Chemical', 'requires_expiry': True, 'default_shelf_life_days': 365})

        moisture, _ = LabTestDefinition.objects.get_or_create(code='DEMO-MOIST', defaults={
            'name': '[DEMO] Moisture content', 'method': 'Oven-dry', 'result_type': 'NUMERIC',
            'unit': units['PCT'], 'min_value': Decimal('0'), 'max_value': Decimal('0.5')})
        purity, _ = LabTestDefinition.objects.get_or_create(code='DEMO-PURITY', defaults={
            'name': '[DEMO] Purity', 'method': 'Titration', 'result_type': 'NUMERIC',
            'unit': units['PCT'], 'min_value': Decimal('98.5')})
        appearance, _ = LabTestDefinition.objects.get_or_create(code='DEMO-APPEAR', defaults={
            'name': '[DEMO] Appearance', 'result_type': 'TEXT',
            'acceptable_text_values': ['White crystalline powder'], 'is_required': False})

        # Business rules: bags counted in tonnes, every bag carries expiry and retest dates.
        materials = [
            ('DEMO-MAT-001', '[DEMO] Sodium carbonate', chem, [moisture, purity, appearance]),
            ('DEMO-MAT-002', '[DEMO] HDPE granules', raw, [moisture]),
            ('DEMO-MAT-003', '[DEMO] Steel billets', raw, []),
        ]
        for code, name, cat, tests in materials:
            m, _ = Material.objects.get_or_create(code=code, defaults={
                'name': name, 'category': cat, 'default_unit': units['T'], 'requires_expiry': True,
                'requires_retest': True, 'shelf_life_days': 365, 'retest_interval_days': 180,
                'allow_partial_issue': False, 'qc_required': bool(tests)})
            for t in tests:
                MaterialTestRequirement.objects.get_or_create(material=m, test_definition=t)

        wh, _ = Warehouse.objects.get_or_create(code='DEMO-WH1', defaults={'name': '[DEMO] Main warehouse'})

        def loc(code, name, kind, parent=None, quarantine=False):
            return StorageLocation.objects.get_or_create(warehouse=wh, code=code, defaults={
                'name': name, 'location_type': kind, 'parent': parent, 'is_quarantine': quarantine})[0]

        zone_a = loc('A', '[DEMO] Zone A', 'ZONE')
        loc('Q', '[DEMO] Quarantine / Pending QC', 'QUARANTINE', quarantine=True)
        for r in (1, 2):
            rack = loc(f'A-R{r}', f'[DEMO] Rack A{r}', 'RACK', zone_a)
            for s in (1, 2, 3):
                loc(f'A-R{r}-S{s}', f'[DEMO] Shelf A{r}-{s}', 'SHELF', rack)

        factory, _ = Factory.objects.get_or_create(code='DEMO-F1', defaults={'name': '[DEMO] Factory 1'})
        Department.objects.get_or_create(factory=factory, code='MIX', defaults={'name': '[DEMO] Mixing'})
        Department.objects.get_or_create(factory=factory, code='EXT', defaults={'name': '[DEMO] Extrusion'})

        created_users = []
        for role in Role:
            username = f'demo_{role.value.lower()}'
            user, created = User.objects.get_or_create(username=username, defaults={
                'email': f'{username}@demo.invalid', 'role': role.value,
                'first_name': '[DEMO]', 'last_name': role.label, 'is_staff': role == Role.ADMIN})
            if created:
                user.set_password(password)
                user.save()
                created_users.append(username)

        if opts['with_sample_flow']:
            self._sample_flow(units)

        self.stdout.write(self.style.SUCCESS('Demo data loaded (all records prefixed DEMO).'))
        if created_users:
            self.stdout.write(f'Created users: {", ".join(created_users)}')
            self.stdout.write('Password: value of DEMO_USER_PASSWORD (default "demo-pass-123!")')

    def _sample_flow(self, units):
        """DEMO receipts at different stages, created through the service layer with the proper roles."""
        from datetime import timedelta
        from django.utils import timezone
        from apps.inventory import services as inv
        from apps.inventory.models import Package
        from apps.laboratory import services as lab
        from apps.receiving import services as rcv
        from apps.receiving.models import GoodsReceipt

        if GoodsReceipt.objects.filter(invoice_number='DEMO-INV-A').exists():
            self.stdout.write('Sample receipts already present; skipped.')
            self._issue_flow(units)
            return
        u = {r: User.objects.get(username=f'demo_{r.lower()}') for r in
             ('GATE', 'RECEIVING', 'WAREHOUSE', 'LAB_TECH', 'LAB_REVIEWER')}
        sup = {s.code: s for s in Supplier.objects.filter(code__startswith='DEMO-SUP-')}
        mat = {m.code: m for m in Material.objects.filter(code__startswith='DEMO-MAT-')}
        loc = {l.code: l for l in StorageLocation.objects.filter(warehouse__code='DEMO-WH1')}
        now = timezone.now()

        def receipt(invoice, supplier, lines, arrived_hours_ago, **weights):
            r = rcv.create_receipt({'supplier': supplier, 'invoice_number': invoice, 'invoice_date': timezone.localdate(),
                                    'vehicle_number': f'OD02DM{invoice[-1]}{arrived_hours_ago:02d}',
                                    'arrival_datetime': now - timedelta(hours=arrived_hours_ago), **weights}, u['GATE'])
            for line in lines:
                rcv.add_invoice_line(r, line, u['GATE'])
            return r

        def unload_and_store(r, qty_each, count):
            for name, who in (('register_at_gate', 'GATE'), ('submit_for_unloading', 'GATE'), ('start_unloading', 'RECEIVING')):
                r = rcv.transition_receipt(r, name, u[who])
            line = r.invoice_lines.get()
            rcv.record_verification(line, {'received_quantity': line.declared_quantity, 'received_unit': line.declared_unit},
                                    u['RECEIVING'])
            pkgs = inv.create_packages(line, [{'quantity': Decimal(qty_each)}] * count, u['RECEIVING'])
            for p in pkgs:
                inv.record_label_print(p, u['RECEIVING'])
            for name in ('mark_verification_pending', 'mark_unloaded'):
                r = rcv.transition_receipt(r, name, u['RECEIVING'])
            for p in pkgs:
                inv.putaway_package(p, loc['Q'], u['WAREHOUSE'])
            return pkgs

        kg, t = units['KG'], units['T']
        today = timezone.localdate()
        dates = {'expiry_date': today + timedelta(days=330), 'retest_date': today + timedelta(days=150)}
        # A — released and on a shelf (available for issue)
        a = receipt('DEMO-INV-A', sup['DEMO-SUP-1'], [{'material': mat['DEMO-MAT-001'], 'declared_quantity': Decimal('1'),
                    'declared_unit': t, 'declared_weight': Decimal('1000'), 'declared_weight_unit': kg,
                    'batch_number': 'DEMO-LOT-A1', **dates}], 30,
                    gross_weight=Decimal('9020'), tare_weight=Decimal('8000'), weight_unit=kg)
        pkgs = unload_and_store(a, '0.25', 4)
        for bag in pkgs:  # one sample per bag
            sample = lab.create_sample({'packages': [bag], 'required_by': now + timedelta(days=1)}, u['LAB_TECH'])
            # No weight drawn, so bags stay exactly 0.25 t and whole-bag issue can meet the demo request.
            lab.collect_sample(sample, {}, u['LAB_TECH'])
            run = lab.start_testing(sample, u['LAB_TECH'])
            tests = {x.code: x for x in sample.requested_tests.all()}
            lab.record_results(run, [
                {'test_definition': tests['DEMO-MOIST'], 'numeric_result': Decimal('0.21')},
                {'test_definition': tests['DEMO-PURITY'], 'numeric_result': Decimal('99.20')},
                {'test_definition': tests['DEMO-APPEAR'], 'text_result': 'White crystalline powder'},
            ], u['LAB_TECH'])
            lab.submit_run(run, u['LAB_TECH'])
            lab.decide(sample, {'decision': 'RELEASED', 'reason': '[DEMO] All tests within specification'},
                       u['LAB_REVIEWER'])
        for p, shelf in zip(pkgs, ('A-R1-S1', 'A-R1-S1', 'A-R1-S2', 'A-R1-S2')):
            inv.transfer_package(p, loc[shelf], u['WAREHOUSE'], notes='[DEMO] released stock to rack')

        self._issue_flow(units)

        # B — in quarantine, sample collected, testing open (results awaited)
        b = receipt('DEMO-INV-B', sup['DEMO-SUP-2'], [{'material': mat['DEMO-MAT-002'], 'declared_quantity': Decimal('0.5'),
                    'declared_unit': t, 'batch_number': 'DEMO-LOT-B1', **dates}], 20)
        for bag in unload_and_store(b, '0.25', 2):
            sample = lab.create_sample({'packages': [bag], 'required_by': now + timedelta(hours=12)}, u['LAB_TECH'])
            lab.collect_sample(sample, {}, u['LAB_TECH'])
            lab.start_testing(sample, u['LAB_TECH'])

        # C — registered at gate, awaiting unloading
        c = receipt('DEMO-INV-C', sup['DEMO-SUP-3'], [{'material': mat['DEMO-MAT-003'], 'declared_quantity': Decimal('2.4'),
                    'declared_unit': t, 'batch_number': 'DEMO-LOT-C1', **dates}], 2)
        for name in ('register_at_gate', 'submit_for_unloading'):
            c = rcv.transition_receipt(c, name, u['GATE'])

        # D — draft at the gate
        receipt('DEMO-INV-D', sup['DEMO-SUP-1'], [], 0)
        self.stdout.write('Sample flow: DEMO-INV-A released stock, DEMO-INV-B in testing, '
                          'DEMO-INV-C awaiting unloading, DEMO-INV-D draft.')

    def _issue_flow(self, units):
        """DEMO factory request DEMO-PO-1001 for 0.75 t: allocated, one 0.25 t bag issued and acknowledged."""
        from apps.inventory import issuing
        from apps.inventory.models import MaterialRequest, Package
        if MaterialRequest.objects.filter(production_order='DEMO-PO-1001').exists():
            return
        factory_user = User.objects.get(username='demo_factory')
        warehouse = User.objects.get(username='demo_warehouse')
        material = Material.objects.get(code='DEMO-MAT-001')
        t = units['T']
        factory = Factory.objects.get(code='DEMO-F1')
        req = issuing.create_request({'factory': factory, 'department': factory.departments.get(code='MIX'),
                                      'production_order': 'DEMO-PO-1001',
                                      'lines': [{'material': material, 'quantity': Decimal('0.75'), 'unit': t}]},
                                     factory_user)
        issuing.allocate_request(req, warehouse)
        picks = issuing.suggest(material, t, Decimal('0.25'), request_line=req.lines.get())['suggestions']
        issue = issuing.create_issue({'material_request': req, 'received_by': factory_user,
                                      'lines': [{'package': Package.objects.get(pk=p['package']), 'quantity': Decimal(p['quantity'])}
                                                for p in picks]}, warehouse)
        issuing.acknowledge_issue(issue, factory_user)
        self.stdout.write(f'Issue flow: request {req.request_number} (DEMO-PO-1001) allocated, {issue.issue_number} issued.')
