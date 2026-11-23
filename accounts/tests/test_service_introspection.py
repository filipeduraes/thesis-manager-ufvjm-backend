"""Tests of the service-to-service identity introspection endpoint."""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import ProfileRole, User, UserProfileRole

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SERVICE_TOKEN="segredo-de-servico")
class ServiceIntrospectionTests(TestCase):
    """Only callers with the service token may resolve user identity."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(email="aluno@ufvjm.edu.br", password="senha123", name="Aluno")
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.STUDENT)
        self.client = APIClient()

    def test_requires_service_token(self) -> None:
        response = self.client.get("/api/v1/auth/introspect", {"user_id": str(self.user.pk)})
        self.assertEqual(response.status_code, 401)

    @override_settings(SERVICE_TOKEN="")
    def test_endpoint_is_disabled_without_a_configured_token(self) -> None:
        response = self.client.get(
            "/api/v1/auth/introspect",
            {"user_id": str(self.user.pk)},
            headers={"X-Service-Token": "local-service-token-change-before-deploy"},
        )
        self.assertEqual(response.status_code, 401)

    def test_returns_identity_and_roles(self) -> None:
        response = self.client.get(
            "/api/v1/auth/introspect",
            {"user_id": str(self.user.pk)},
            headers={"X-Service-Token": "segredo-de-servico"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["roles"], [ProfileRole.STUDENT])
        self.assertEqual(response.data["name"], "Aluno")
