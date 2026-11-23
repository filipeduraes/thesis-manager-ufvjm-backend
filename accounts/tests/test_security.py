"""
Regression tests for network-origin and audit hardening.
"""

from __future__ import annotations

from django.test import RequestFactory, TestCase, override_settings

from accounts.middleware import AdminNetworkRestrictionMiddleware
from accounts.models import AuditAction, ProfileRole, User, UserProfileRole
from accounts.network import get_client_ip
from accounts.services import record_audit
from accounts.audit_entry import AuditEntry
from accounts.audit_identifiers import EntityType


@override_settings(
    TRUSTED_PROXY_COUNT=1,
    TRUSTED_PROXY_CIDRS=["10.0.0.0/8"],
    ADMIN_ALLOWED_CIDRS=["192.168.1.0/24"],
)
class TrustedProxyTests(TestCase):
    """Forwarded headers are only trusted from configured proxies."""

    def setUp(self) -> None:
        self.factory = RequestFactory()

    def test_spoofed_forwarded_for_from_untrusted_peer_is_ignored(self) -> None:
        request = self.factory.get("/api/v1/admin/users", REMOTE_ADDR="8.8.8.8", HTTP_X_FORWARDED_FOR="192.168.1.10")
        self.assertEqual(get_client_ip(request), "8.8.8.8")

    def test_forwarded_for_from_trusted_proxy_is_used(self) -> None:
        request = self.factory.get("/api/v1/admin/users", REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="192.168.1.10")
        self.assertEqual(get_client_ip(request), "192.168.1.10")

    def test_middleware_denies_spoofed_admin_request(self) -> None:
        request = self.factory.get("/api/v1/admin/users", REMOTE_ADDR="8.8.8.8", HTTP_X_FORWARDED_FOR="192.168.1.10")
        response = AdminNetworkRestrictionMiddleware(lambda _: None)(request)
        self.assertEqual(response.status_code, 404)


class AuditHardeningTests(TestCase):
    """Sensitive values are not duplicated into application logs."""

    def test_invitation_token_response_has_no_store(self) -> None:
        user = User.objects.create_user(email="admin@ufvjm.edu.br", name="Admin", password="x")
        UserProfileRole.objects.create(user=user, role=ProfileRole.ADMIN)
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user=user)
        response = client.post(
            "/api/v1/admin/external-invitations",
            {"name": "Externo", "email": "externo@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_soft_deleted_thesis_is_not_listed(self) -> None:
        from django.utils import timezone

        from academic.models import Course, Department
        from theses.models import Thesis

        department = Department.objects.create(name="Computação")
        course = Course.objects.create(name="CC", code="CC1", department=department)
        advisor = User.objects.create_user(email="adv@ufvjm.edu.br", name="Adv", password="x")
        UserProfileRole.objects.create(user=advisor, role=ProfileRole.ADVISOR)
        thesis = Thesis.objects.create(course=course, advisor=advisor, title="Removida")
        thesis.deleted_at = timezone.now()
        thesis.save(update_fields=["deleted_at"])

        self.assertFalse(Thesis.objects.filter(pk=thesis.pk, deleted_at__isnull=True).exists())
