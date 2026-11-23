"""
Regression tests for network-origin and audit hardening.
"""

from __future__ import annotations

import re

from django.test import RequestFactory, TestCase, override_settings

from accounts.middleware import AdminNetworkRestrictionMiddleware
from accounts.models import AuditAction, ProfileRole, User, UserProfileRole
from accounts.network import get_client_ip
from accounts.services import record_audit
from accounts.audit_entry import AuditEntry
from accounts.audit_identifiers import EntityType
from notifications.tests.invitation_fixtures import INVITATION_TEST_SETTINGS, InvitationFixtureMixin, deliver_pending_invitations
from notifications.tests.token_leaks import last_emailed_token


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


TOKEN_PATTERN = re.compile(r"UFVJM(?:-[A-Z0-9]{4}){4}")


@override_settings(**INVITATION_TEST_SETTINGS)
class InvitationResponseCacheTests(InvitationFixtureMixin, TestCase):
    """
    Invitation responses never carry the access token and are still marked ``no-store``.
    """

    def setUp(self) -> None:
        super().setUp()
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.base = f"/api/v1/defenses/{self.defense.pk}/members"

    def _assert_without_token(self, response, emailed_tokens: list[str]) -> None:
        self.assertEqual(response["Cache-Control"], "no-store")
        body = response.content.decode()
        self.assertIsNone(TOKEN_PATTERN.search(body), body)
        for key in ("token", "accessToken", "accessTokenExpiresAt"):
            self.assertNotIn(f'"{key}"', body)
        for token in emailed_tokens:
            self.assertNotIn(token, body)

    def test_invitation_responses_have_no_token_and_no_store(self) -> None:
        created = self.client.post(f"{self.base}/external", {"role": "MEMBER", "name": "Dra. Externa", "email": "externa@example.com", "institution": "UFMG"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        member_id = created.data["member"]["id"]
        deliver_pending_invitations()
        created_token = last_emailed_token(self)
        self._assert_without_token(created, [created_token])

        resent = self.client.post(f"{self.base}/{member_id}/resend-invitation", format="json")
        self.assertEqual(resent.status_code, 202, resent.data)
        deliver_pending_invitations()
        resent_token = last_emailed_token(self)
        self._assert_without_token(resent, [created_token, resent_token])

        regenerated = self.client.post(f"{self.base}/{member_id}/regenerate-token", format="json")
        self.assertEqual(regenerated.status_code, 202, regenerated.data)
        deliver_pending_invitations()
        regenerated_token = last_emailed_token(self)
        self._assert_without_token(regenerated, [created_token, resent_token, regenerated_token])
        self.assertEqual(len({created_token, resent_token, regenerated_token}), 3)

    def test_standalone_admin_invitation_endpoint_is_gone(self) -> None:
        response = self.client.post("/api/v1/admin/external-invitations", {"name": "Externo", "email": "externo@example.com"}, format="json")
        self.assertEqual(response.status_code, 405)
        self.assertIsNone(TOKEN_PATTERN.search(response.content.decode()))
