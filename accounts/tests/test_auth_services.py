"""
Tests for authentication services.
"""
from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.utils import timezone

from accounts.models import ProfileRole, UserProfileRole
from accounts.services import (
    ExternalTokenError,
    LastAdminError,
    active_role_codes,
    create_external_invitation,
    exchange_external_token,
    get_active_role,
    set_active_role,
    set_user_roles,
)

User = get_user_model()


class TestSession(dict):
    """Small session substitute for testing service-level token exchange."""
    def flush(self) -> None:
        self.clear()

    def set_expiry(self, seconds: int) -> None:
        self["_session_expiry"] = seconds


class RoleServicesTests(TestCase):
    """
    Test role assignment and session selection.
    """
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno"
        )
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.STUDENT)

    def test_active_role_codes_follow_priority(self) -> None:
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.ADVISOR)
        UserProfileRole.objects.create(user=self.user, role=ProfileRole.ADMIN)
        self.assertEqual(
            active_role_codes(self.user),
            [ProfileRole.ADMIN, ProfileRole.ADVISOR, ProfileRole.STUDENT],
        )

    def test_set_active_role_validates_assignment(self) -> None:
        request = self.factory.get("/")
        request.session = TestSession()
        with self.assertRaises(ValidationError):
            set_active_role(self.user, ProfileRole.ADMIN, request)
        self.user.is_staff = True
        self.user.save()
        set_user_roles(self.user, [ProfileRole.ADMIN], assigned_by=self.user)
        active_role = set_active_role(self.user, ProfileRole.ADMIN, request)
        self.assertEqual(active_role, ProfileRole.ADMIN)
        self.assertEqual(get_active_role(self.user, request), ProfileRole.ADMIN)


class AdminProtectionTests(TestCase):
    """
    Test last-administrator safeguards.
    """
    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.admin = User.objects.create_superuser(
            email="admin@ufvjm.edu.br", name="Admin", password="pass"
        )
        self.user = User.objects.create_user(
            email="aluno@ufvjm.edu.br", name="Aluno"
        )

    def test_cannot_remove_last_admin_role(self) -> None:
        with self.assertRaises(LastAdminError):
            set_user_roles(self.admin, [ProfileRole.STUDENT], assigned_by=self.admin)

    def test_cannot_deactivate_last_admin(self) -> None:
        from accounts.services import deactivate_user

        with self.assertRaises(LastAdminError):
            deactivate_user(self.admin, actor=self.admin)


class ExternalTokenTests(TestCase):
    """
    Test external invitation token lifecycle.
    """
    def setUp(self) -> None:
        self.factory = RequestFactory()

    def test_exchange_external_token_clears_session_and_marks_invitation(self) -> None:
        invitation, token = create_external_invitation(
            name="Membro Externo",
            email="externo@outra.org",
            institution="UFMG",
        )
        request = self.factory.post("/api/v1/auth/external-token")
        request.session = TestSession()
        exchanged = exchange_external_token(token, request)
        invitation.refresh_from_db()
        self.assertEqual(exchanged.pk, invitation.pk)
        self.assertEqual(invitation.status, "USED")
        self.assertIsNotNone(invitation.used_at)
        self.assertIn("external_invitation_id", request.session)
        self.assertNotIn("_auth_user_id", request.session)
        with self.assertRaises(ExternalTokenError):
            exchange_external_token(token, request)

    def test_exchange_rejects_unknown_token(self) -> None:
        from accounts.services import ExternalTokenError

        request = self.factory.post("/api/v1/auth/external-token")
        request.session = {}
        with self.assertRaises(ExternalTokenError):
            exchange_external_token("UFVJM-AAAA-BBBB-CCCC-DDDD", request)
