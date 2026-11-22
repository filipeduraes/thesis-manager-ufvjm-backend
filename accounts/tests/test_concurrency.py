"""
Testes das proteções de concorrência aplicadas em serviços de contas.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from accounts.audit_identifiers import EntityType
from accounts.audit_entry import AuditEntry
from accounts.models import AuditAction, AuditLog
from accounts.services import record_audit


class AuditChainConcurrencyTests(TestCase):
    """A cadeia de hash é serializada e permanece íntegra entre escritas."""

    @patch("accounts.services.acquire_transaction_lock")
    def test_audit_append_uses_transaction_lock(self, acquire: object) -> None:
        record_audit(AuditEntry(
            action=AuditAction.LOGIN,
            entity_type=EntityType.USER,
            entity_id="00000000-0000-0000-0000-000000000001",
        ))
        acquire.assert_called_once()  # type: ignore[attr-defined]

    def test_consecutive_entries_form_a_single_chain(self) -> None:
        first = record_audit(AuditEntry(
            action=AuditAction.LOGIN,
            entity_type=EntityType.USER,
            entity_id="00000000-0000-0000-0000-000000000001",
        ))
        second = record_audit(AuditEntry(
            action=AuditAction.LOGOUT,
            entity_type=EntityType.USER,
            entity_id="00000000-0000-0000-0000-000000000001",
        ))
        self.assertEqual(bytes(second.previous_hash), bytes(first.row_hash))
        self.assertEqual(AuditLog.objects.count(), 2)
