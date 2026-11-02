from django.test import TestCase

from apps.core.testing import make_user, client_for
from apps.master.models import Supplier


class MasterDataPermissionTests(TestCase):
    def setUp(self):
        self.admin = make_user('ADMIN')
        self.gate = make_user('GATE')

    def test_any_authenticated_user_can_read(self):
        Supplier.objects.create(code='S1', name='One')
        resp = client_for(self.gate).get('/api/v1/master/suppliers/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['count'], 1)

    def test_roles_outside_the_create_list_cannot_add(self):
        payload = {'code': 'S2', 'name': 'Two'}
        lab = make_user('LAB_TECH')
        self.assertEqual(client_for(lab).post('/api/v1/master/suppliers/', payload, format='json').status_code, 403)
        self.assertEqual(client_for(self.admin).post('/api/v1/master/suppliers/', payload, format='json').status_code, 201)

    def test_location_full_path(self):
        client = client_for(self.admin)
        wh = client.post('/api/v1/master/warehouses/', {'code': 'W', 'name': 'W'}, format='json').json()
        zone = client.post('/api/v1/master/locations/', {'warehouse': wh['id'], 'code': 'Z', 'name': 'Z', 'location_type': 'ZONE'}, format='json').json()
        rack = client.post('/api/v1/master/locations/', {'warehouse': wh['id'], 'parent': zone['id'], 'code': 'R1', 'name': 'R1', 'location_type': 'RACK'}, format='json').json()
        self.assertEqual(rack['full_path'], 'Z > R1')


class InlineMasterCreateTests(TestCase):
    """'+ New' next to a dropdown: permitted roles add records; only admins edit; additions are audited."""

    def setUp(self):
        from apps.core.testing import Fixtures
        self.fx = Fixtures()

    def as_(self, role):
        from apps.core.testing import client_for
        return client_for(self.fx.users[role])

    def test_gate_adds_supplier_and_material_but_not_units_or_locations(self):
        from apps.core.models import AuditEvent
        resp = self.as_('GATE').post('/api/v1/master/suppliers/', {'code': 'SUP-NEW', 'name': 'New Supplier'}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(AuditEvent.objects.filter(entity_type='Supplier', entity_id=str(resp.json()['id']), action='CREATE').exists())
        resp = self.as_('GATE').post('/api/v1/master/materials/', {'code': 'MAT-NEW', 'name': 'New material',
                                                                   'default_unit': self.fx.tonne.pk}, format='json')
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertTrue(body['qc_required'] and body['requires_expiry'] and body['requires_retest'])  # safe defaults
        self.assertEqual(self.as_('GATE').post('/api/v1/master/units/', {'code': 'X', 'name': 'X', 'symbol': 'x'}, format='json').status_code, 403)
        self.assertEqual(self.as_('GATE').post('/api/v1/master/locations/', {'code': 'Z', 'name': 'Z', 'warehouse': self.fx.warehouse.pk}, format='json').status_code, 403)

    def test_only_admin_edits_and_roles_follow_configuration(self):
        from apps.core.testing import set_config
        sup = self.as_('GATE').post('/api/v1/master/suppliers/', {'code': 'S9', 'name': 'S9'}, format='json').json()
        self.assertEqual(self.as_('GATE').patch(f"/api/v1/master/suppliers/{sup['id']}/", {'name': 'x'}, format='json').status_code, 403)
        self.assertEqual(self.as_('ADMIN').patch(f"/api/v1/master/suppliers/{sup['id']}/", {'name': 'x'}, format='json').status_code, 200)
        # Managers maintain master data too: edit, and add every kind (also outside master.create_roles)
        self.assertEqual(self.as_('MANAGER').patch(f"/api/v1/master/suppliers/{sup['id']}/", {'is_active': False}, format='json').status_code, 200)
        self.assertEqual(self.as_('MANAGER').post('/api/v1/master/warehouses/', {'code': 'WH2', 'name': 'Second'}, format='json').status_code, 201)
        self.assertEqual(self.as_('WAREHOUSE').patch(f"/api/v1/master/suppliers/{sup['id']}/", {'name': 'y'}, format='json').status_code, 403)
        self.assertEqual(self.as_('WAREHOUSE').post('/api/v1/master/locations/', {'code': 'B1', 'name': 'Bin 1', 'warehouse': self.fx.warehouse.pk}, format='json').status_code, 201)
        set_config(**{'master__create_roles': {'supplier': ['ADMIN']}})
        self.assertEqual(self.as_('GATE').post('/api/v1/master/suppliers/', {'code': 'S10', 'name': 'S10'}, format='json').status_code, 403)
        policy = self.as_('FACTORY').get('/api/v1/policy/').json()
        self.assertIn('master.create_roles', policy)
        self.assertEqual(policy['master.manage_roles'], ['ADMIN', 'MANAGER'])


class MasterDataAdminTests(TestCase):
    """The in-app Master data screens: full edit, lab tests on materials, deactivate, safe delete, audit."""

    def setUp(self):
        from apps.core.testing import Fixtures, client_for
        self.fx = Fixtures()
        self.admin = client_for(self.fx.users['ADMIN'])

    def test_material_lab_tests_are_set_and_replaced(self):
        from apps.master.models import LabTestDefinition
        a = LabTestDefinition.objects.create(code='A', name='A')
        b = LabTestDefinition.objects.create(code='B', name='B')
        m = self.admin.post('/api/v1/master/materials/', {'code': 'M9', 'name': 'M9', 'default_unit': self.fx.tonne.pk,
                                                          'test_ids': [a.pk, b.pk]}, format='json').json()
        self.assertEqual(sorted(t['test_definition'] for t in m['test_requirements']), [a.pk, b.pk])
        m = self.admin.patch(f"/api/v1/master/materials/{m['id']}/", {'test_ids': [b.pk]}, format='json').json()
        self.assertEqual([t['test_definition'] for t in m['test_requirements']], [b.pk])
        m = self.admin.patch(f"/api/v1/master/materials/{m['id']}/", {'name': 'Renamed'}, format='json').json()
        self.assertEqual(len(m['test_requirements']), 1)  # untouched when not sent

    def test_edit_is_audited_and_deactivate_hides_from_active_lists(self):
        from apps.core.models import AuditEvent
        sup = self.fx.supplier
        self.admin.patch(f'/api/v1/master/suppliers/{sup.pk}/', {'phone': '98765', 'is_active': False}, format='json')
        ev = AuditEvent.objects.get(entity_type='Supplier', entity_id=str(sup.pk), action='UPDATE')
        self.assertEqual(ev.before_state['is_active'], 'True')
        self.assertEqual(ev.after_state, {'phone': '98765', 'is_active': 'False'})
        codes = [s['code'] for s in self.admin.get('/api/v1/master/suppliers/', {'is_active': 'true'}).json()['results']]
        self.assertNotIn(sup.code, codes)
        self.assertEqual(self.admin.get('/api/v1/master/suppliers/', {'search': 'SUP-1'}).json()['count'], 1)

    def test_delete_only_unused_records(self):
        unused = self.admin.post('/api/v1/master/suppliers/', {'code': 'TMP', 'name': 'Temp'}, format='json').json()
        self.assertEqual(self.admin.delete(f"/api/v1/master/suppliers/{unused['id']}/").status_code, 204)
        self.fx.receipt_in_unloading()  # uses fx.supplier
        resp = self.admin.delete(f'/api/v1/master/suppliers/{self.fx.supplier.pk}/')
        self.assertEqual(resp.status_code, 409)
        self.assertIn('Deactivate it instead', resp.json()['message'])


class MasterSearchTests(TestCase):
    """Master lists are searchable on every meaningful field, not just code and name."""

    def setUp(self):
        from apps.core.testing import Fixtures, client_for
        from apps.master.models import LabTestDefinition, MaterialTestRequirement
        self.fx = Fixtures()
        self.c = client_for(self.fx.users['GATE'])
        Supplier.objects.filter(pk=self.fx.supplier.pk).update(gst_number='21ABCDE1234F1Z5', phone='9437000111',
                                                               contact_person='Anil Das', address='Cuttack')
        t = LabTestDefinition.objects.create(code='ASH', name='Ash content', method='Furnace')
        MaterialTestRequirement.objects.create(material=self.fx.material, test_definition=t)

    def codes(self, path, q):
        return [r['code'] for r in self.c.get(f'/api/v1/master/{path}/', {'search': q}).json()['results']]

    def test_search_any_field(self):
        for q in ('21ABCDE', '9437000', 'anil', 'cuttack'):
            self.assertEqual(self.codes('suppliers', q), [self.fx.supplier.code], q)
        self.assertEqual(self.codes('materials', 'ash content'), [self.fx.material.code])  # by its lab test
        self.assertIn('Q1', self.codes('locations', 'quarantine'))          # by type
        self.assertIn('A-1', self.codes('locations', 'Warehouse'))          # by warehouse name
        self.assertEqual(self.codes('test-definitions', 'furnace'), ['ASH'])  # by method
