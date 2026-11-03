"""
Tests for accounts models and managers.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import (
    AuditAction,
    AuditLog,
    ExternalInvitation,
    InvitationStatus,
    NetworkOrigin,
    ProfileRole,
    UserProfileRole,
)

User = get_user_model()


class UserManagerTests(TestCase):
    """
    Test custom user creation behavior.
    """
    def test_create_user_normalizes_email_and_sets_unusable_password_without_password(self,) -> None:
        user = User.objects.create_user(
            email="Aluno@UFVJM.EDU.BR",
            name="Aluno Teste",
        )
        self.assertEqual(user.email, "aluno@ufvjm.edu.br")
        self.assertFalse(user.has_usable_password())
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_create_superuser_requires_password(self) -> None:
        with self.assertRaises(ValueError):
            User.objects.create_superuser(email="admin@ufvjm.edu.br", name="Admin")

    def test_deactivate_revokes_roles(self) -> None:
        user = User.objects.create_user(email="a@ufvjm.edu.br", name="A")
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        UserProfileRole.objects.create(user=user, role=ProfileRole.ADVISOR)
        User.objects.deactivate(user)
        user.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(
            user.profile_roles.filter(revoked_at__isnull=True).count(),
            0,
        )


class UserProfileRoleTests(TestCase):
    """
    Test role history constraints.
    """
    def test_only_one_active_role_of_same_type_per_user(self) -> None:
        user = User.objects.create_user(email="a@ufvjm.edu.br", name="A")
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        with self.assertRaises(Exception):
            UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)


class ExternalInvitationTests(TestCase):
    """
    Test invitation state helpers.
    """
    def test_is_usable_depends_on_status_expiration_and_revocation(self) -> None:
        invitation = ExternalInvitation.objects.create(
            name="Membro",
            email="membro@exemplo.com",
            token_hash=hashlib.sha256(b"token").digest(),
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.assertTrue(invitation.is_usable)
        invitation.status = InvitationStatus.USED
        self.assertFalse(invitation.is_usable)
        invitation.status = InvitationStatus.PENDING
        invitation.expires_at = timezone.now() - timedelta(minutes=1)
        self.assertFalse(invitation.is_usable)


class AuditLogTests(TestCase):
    """
    Test append-only behavior of the audit log.
    """
    def test_audit_log_is_append_only(self) -> None:
        user = User.objects.create_user(email="a@ufvjm.edu.br", name="A")
        log = AuditLog.objects.create(
            actor_user=user,
            action=AuditAction.LOGIN,
            entity_type="User",
            entity_id=str(user.pk),
            entity_app_label="accounts",
            origin=NetworkOrigin.INSTITUTIONAL_NETWORK,
            ip_address="192.168.1.10",
            user_agent="test",
            changes={},
            metadata={},
            previous_hash=None,
            row_hash=hashlib.sha256(b"row").digest(),
            occurred_at=timezone.now(),
        )
        with self.assertRaises(ValidationError):
            log.action = AuditAction.LOGOUT
            log.save()
        with self.assertRaises(ValidationError):
            log.delete()
