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

    def test_only_admin_can_write(self):
        payload = {'code': 'S2', 'name': 'Two'}
        self.assertEqual(client_for(self.gate).post('/api/v1/master/suppliers/', payload, format='json').status_code, 403)
        self.assertEqual(client_for(self.admin).post('/api/v1/master/suppliers/', payload, format='json').status_code, 201)

    def test_location_full_path(self):
        client = client_for(self.admin)
        wh = client.post('/api/v1/master/warehouses/', {'code': 'W', 'name': 'W'}, format='json').json()
        zone = client.post('/api/v1/master/locations/', {'warehouse': wh['id'], 'code': 'Z', 'name': 'Z', 'location_type': 'ZONE'}, format='json').json()
        rack = client.post('/api/v1/master/locations/', {'warehouse': wh['id'], 'parent': zone['id'], 'code': 'R1', 'name': 'R1', 'location_type': 'RACK'}, format='json').json()
        self.assertEqual(rack['full_path'], 'Z > R1')
