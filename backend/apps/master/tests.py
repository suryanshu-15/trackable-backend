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
        self.assertEqual(self.as_('WAREHOUSE').post('/api/v1/master/locations/', {'code': 'B1', 'name': 'Bin 1', 'warehouse': self.fx.warehouse.pk}, format='json').status_code, 201)
        set_config(**{'master__create_roles': {'supplier': ['ADMIN']}})
        self.assertEqual(self.as_('GATE').post('/api/v1/master/suppliers/', {'code': 'S10', 'name': 'S10'}, format='json').status_code, 403)
        self.assertIn('master.create_roles', self.as_('FACTORY').get('/api/v1/policy/').json())
