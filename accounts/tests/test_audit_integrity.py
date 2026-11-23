"""
Tests of the audit hash chain verification command.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.audit_entry import AuditEntry
from accounts.audit_identifiers import EntityType
from accounts.audit_integrity import verify_audit_chain
from accounts.models import AuditAction, User, UserProfileRole
from accounts.services import record_audit


class AuditIntegrityTests(TestCase):
    """Detect changes to stored rows and to chain links."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(email="admin@ufvjm.edu.br", password="senha123", name="Admin")
        UserProfileRole.objects.create(user=self.user, role="ADMIN")

    def append(self) -> None:
        """Append one audit entry."""
        record_audit(AuditEntry(
            action=AuditAction.UPDATE,
            entity_type=EntityType.USER,
            entity_id=str(self.user.pk),
            actor_user=self.user,
            metadata={"step": "ok"},
        ))

    def test_healthy_chain_is_ok(self) -> None:
        self.append()
        self.append()
        report = verify_audit_chain()
        self.assertEqual(report.checked, 2)
        self.assertTrue(report.ok, f"linhas={report.broken_rows} ligações={report.broken_links}")
        output = StringIO()
        call_command("verify_audit_chain", stdout=output)
        self.assertIn("íntegra", output.getvalue())

    def test_tampered_row_is_detected(self) -> None:
        self.append()
        from accounts.models import AuditLog
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(f'UPDATE {AuditLog._meta.db_table} SET row_hash = %s', [b"\x00" * 32])
        report = verify_audit_chain()
        self.assertEqual(len(report.broken_rows), 1)
        self.assertFalse(report.ok)
        with self.assertRaises(CommandError):
            call_command("verify_audit_chain", stdout=StringIO())
