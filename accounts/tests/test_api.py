"""
Tests for the versioned API endpoints.
"""
from __future__ import annotations

from unittest import mock

from allauth.socialaccount.models import SocialAccount
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import ProfileRole, UserProfileRole
from accounts.services import create_external_invitation

User = get_user_model()
TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accounts-test-cache",
    }
}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class SessionApiTests(TestCase):
    """
    Test /api/v1/session behavior.
    """
    def setUp(self) -> None:
        self.client = APIClient()

    def test_anonymous_session_returns_authenticated_false(self) -> None:
        response = self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["authenticated"])

    def test_authenticated_session_returns_user_and_roles(self) -> None:
        user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno Teste"
        )
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        UserProfileRole.objects.create(user=user, role=ProfileRole.ADVISOR)
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["user"]["roleCode"], ProfileRole.ADVISOR)
        self.assertEqual(data["user"]["roleLabel"], "Orientador")
        self.assertEqual(len(data["user"]["roles"]), 2)
        self.assertNotIn("role", data)
        self.assertNotIn("profile", data)
        self.assertNotIn("permissions", data)

    def test_authenticated_session_returns_google_avatar(self) -> None:
        user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno Teste"
        )
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        SocialAccount.objects.create(
            user=user,
            provider="google",
            uid="google-123",
            extra_data={"picture": "https://lh3.googleusercontent.com/a/test"},
        )
        self.client.force_authenticate(user=user)
        response = self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["authType"], "google")
        self.assertEqual(
            data["user"]["avatarUrl"],
            "https://lh3.googleusercontent.com/a/test",
        )

    def test_logout_clears_session_and_accepts_csrf_from_payload(self) -> None:
        user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno Teste"
        )
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        client = APIClient(enforce_csrf_checks=True)
        client.force_login(user)

        session_response = client.get("/api/v1/session")
        csrf_token = session_response.json()["csrfToken"]
        self.assertTrue(csrf_token)

        response = client.delete(
            "/api/v1/session",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 204)

        after_logout = client.get("/api/v1/session")
        self.assertFalse(after_logout.json()["authenticated"])

    def test_active_profile_switching_is_not_available(self) -> None:
        user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno Teste"
        )
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        self.client.force_authenticate(user=user)
        response = self.client.patch(
            "/api/v1/session/active-profile",
            {"role": ProfileRole.ADMIN},
            format="json",
        )
        self.assertEqual(response.status_code, 404)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class AdminUsersApiTests(TestCase):
    """
    Test administrative user endpoints.
    """
    def setUp(self) -> None:
        self.client = APIClient()
        self.admin = User.objects.create_superuser(
            email="admin@ufvjm.edu.br", name="Admin", password="pass"
        )
        UserProfileRole.objects.create(user=self.admin, role=ProfileRole.ADMIN)

    @override_settings(ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
    def test_admin_users_requires_network_and_permission(self) -> None:
        user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno"
        )
        self.client.force_authenticate(user=user)
        response = self.client.get(
            "/api/v1/admin/users", REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
    def test_admin_can_list_users(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(
            "/api/v1/admin/users", REMOTE_ADDR="127.0.0.1"
        )
        self.assertEqual(response.status_code, 200)

    @override_settings(ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
    def test_admin_can_create_user(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/admin/users",
            {
                "name": "Novo Usuário",
                "email": "novo@ufvjm.edu.br",
                "role": "Orientador",
            },
            REMOTE_ADDR="127.0.0.1",
            format="json",
        )
        self.assertIn(response.status_code, [200, 201])

    @override_settings(ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
    def test_admin_delete_deactivates_user_not_remove(self) -> None:
        target = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno"
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.delete(
            f"/api/v1/admin/users/{target.pk}",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 204)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    @override_settings(ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
    def test_admin_can_issue_and_revoke_external_invitation(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            "/api/v1/admin/external-invitations",
            {
                "name": "Membro Externo",
                "email": "membro@outra.org",
                "institution": "Outra instituição",
            },
            REMOTE_ADDR="127.0.0.1",
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()["token"].startswith("UFVJM-"))
        invitation_id = response.json()["id"]

        revoke_response = self.client.delete(
            f"/api/v1/admin/external-invitations/{invitation_id}",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(revoke_response.status_code, 204)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ExternalTokenApiTests(TestCase):
    """
    Test external token exchange endpoint.
    """
    def setUp(self) -> None:
        self.client = APIClient()

    def test_external_token_exchange_returns_200_with_session(self) -> None:
        invitation, token = create_external_invitation(
            name="Banca Externa",
            email="banca@avaliador.org",
        )
        response = self.client.post(
            "/api/v1/auth/external-token",
            {"token": token},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["authenticated"])
        self.assertEqual(response.json()["authType"], "external")

        replay_response = self.client.post(
            "/api/v1/auth/external-token",
            {"token": token},
            format="json",
        )
        self.assertEqual(replay_response.status_code, 401)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", DEBUG=True)
class DevLoginApiTests(TestCase):
    """
    Testa o login de usuário de teste, disponível apenas em desenvolvimento.
    """

    def setUp(self) -> None:
        self.client = APIClient()

    @override_settings(DEBUG=False)
    def test_dev_login_is_unavailable_when_debug_is_false(self) -> None:
        response = self.client.post(
            "/api/v1/auth/dev-login",
            {"name": "Teste", "email": "teste@local", "role": "STUDENT"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(DEBUG=False)
    def test_dev_users_is_unavailable_when_debug_is_false(self) -> None:
        response = self.client.get("/api/v1/auth/dev-users")
        self.assertEqual(response.status_code, 404)

    def test_dev_login_creates_user_and_authenticated_session(self) -> None:
        response = self.client.post(
            "/api/v1/auth/dev-login",
            {"name": "Aluno Teste", "email": "aluno.teste@local", "role": "STUDENT"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["authenticated"])
        self.assertEqual(data["user"]["email"], "aluno.teste@local")
        self.assertEqual(data["user"]["roleCode"], ProfileRole.STUDENT)
        user = User.objects.get(email="aluno.teste@local")
        self.assertTrue(user.has_active_role(ProfileRole.STUDENT))

        session_response = self.client.get("/api/v1/session")
        self.assertTrue(session_response.json()["authenticated"])

    def test_dev_login_reuses_existing_user_and_adds_role(self) -> None:
        user = User.objects.create_user(email="orientador@local", name="Antigo")
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        response = self.client.post(
            "/api/v1/auth/dev-login",
            {"name": "Orientador Teste", "email": "orientador@local", "role": "ADVISOR"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user"]["roleCode"], ProfileRole.ADVISOR)
        user.refresh_from_db()
        self.assertEqual(user.name, "Orientador Teste")
        self.assertTrue(user.has_active_role(ProfileRole.ADVISOR))
        self.assertFalse(user.has_active_role(ProfileRole.STUDENT))

    def test_dev_users_lists_active_users(self) -> None:
        user = User.objects.create_user(email="lista@local", name="Lista Teste")
        UserProfileRole.objects.create(user=user, role=ProfileRole.COORDINATOR)
        response = self.client.get("/api/v1/auth/dev-users")
        self.assertEqual(response.status_code, 200)
        emails = [item["email"] for item in response.json()["results"]]
        self.assertIn("lista@local", emails)
