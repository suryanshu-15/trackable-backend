from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.models import AuditEvent
from apps.core.testing import make_user, client_for


class AuthTests(TestCase):
    def setUp(self):
        self.user = make_user('GATE', first_name='Gita')

    def test_login_returns_tokens_and_role(self):
        resp = APIClient().post('/api/v1/auth/login/', {'username': 'test_gate', 'password': 'Test-pass-123!'}, format='json')
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn('access', body)
        self.assertIn('refresh', body)
        self.assertEqual(body['user']['role'], 'GATE')

    def test_login_bad_password(self):
        resp = APIClient().post('/api/v1/auth/login/', {'username': 'test_gate', 'password': 'nope'}, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_inactive_user_cannot_log_in(self):
        self.user.is_active = False
        self.user.save()
        resp = APIClient().post('/api/v1/auth/login/', {'username': 'test_gate', 'password': 'Test-pass-123!'}, format='json')
        self.assertEqual(resp.status_code, 401)

    def test_me_and_bearer_token(self):
        login = APIClient().post('/api/v1/auth/login/', {'username': 'test_gate', 'password': 'Test-pass-123!'}, format='json')
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login.json()['access']}")
        resp = client.get('/api/v1/auth/me/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['username'], 'test_gate')

    def test_user_cannot_change_own_role_via_me(self):
        client = client_for(self.user)
        client.patch('/api/v1/auth/me/', {'role': 'ADMIN'}, format='json')
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, 'GATE')


class UserAdminApiTests(TestCase):
    def setUp(self):
        self.admin = make_user('ADMIN')
        self.gate = make_user('GATE')

    def test_non_admin_cannot_list_users(self):
        self.assertEqual(client_for(self.gate).get('/api/v1/auth/users/').status_code, 403)

    def test_admin_set_role_is_audited(self):
        resp = client_for(self.admin).post(
            f'/api/v1/auth/users/{self.gate.pk}/set-role/', {'role': 'WAREHOUSE', 'reason': 'transfer'}, format='json')
        self.assertEqual(resp.status_code, 200)
        event = AuditEvent.objects.get(entity_type='User', entity_id=str(self.gate.pk))
        self.assertEqual(event.before_state, {'role': 'GATE'})
        self.assertEqual(event.after_state, {'role': 'WAREHOUSE'})

    def test_users_cannot_be_deleted(self):
        resp = client_for(self.admin).delete(f'/api/v1/auth/users/{self.gate.pk}/')
        self.assertEqual(resp.status_code, 405)

    def test_admin_cannot_deactivate_self(self):
        resp = client_for(self.admin).post(f'/api/v1/auth/users/{self.admin.pk}/deactivate/')
        self.assertEqual(resp.status_code, 400)
