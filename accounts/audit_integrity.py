"""
Integrity verification of the append-only audit hash chain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from uuid import UUID

from accounts.audit_payload import AuditPayload
from accounts.models import AuditLog


@dataclass(frozen=True)
class AuditIntegrityReport:
    """
    Result of verifying every row of the audit chain.

    Attributes:
        checked: Number of rows examined.
        broken_rows: Identifiers whose stored hash differs from the recomputed one.
        broken_links: Identifiers whose ``previous_hash`` does not match the predecessor.
    """

    checked: int
    broken_rows: list[UUID] = field(default_factory=list)
    broken_links: list[UUID] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return True when no tampering was detected."""
        return not self.broken_rows and not self.broken_links


def _row_payload(row: AuditLog, previous_hash_hex: str | None) -> AuditPayload:
    """
    Rebuild the canonical payload hashed when the row was appended.

    Args:
        row: Stored audit entry.
        previous_hash_hex: Hex digest of the preceding row.

    Returns:
        AuditPayload: Payload used to recompute ``row_hash``.
    """
    return AuditPayload(
        action=str(row.action),
        entity_type=str(row.entity_type),
        entity_id=str(row.entity_id),
        entity_app_label=str(row.entity_app_label),
        origin=str(row.origin),
        ip_address=row.ip_address,
        user_agent=row.user_agent or "",
        changes=row.changes or {},
        metadata=row.metadata or {},
        occurred_at=row.occurred_at,
        previous_hash_hex=previous_hash_hex,
        actor_user_id=str(row.actor_user_id) if row.actor_user_id else None,
        actor_external_invitation_id=str(row.actor_external_invitation_id) if row.actor_external_invitation_id else None,
        actor_role=str(row.actor_role) if row.actor_role else None,
        actor_roles=tuple(row.actor_roles or ()),
        category=str(row.category or ""),
    )


def verify_audit_chain() -> AuditIntegrityReport:
    """
    Recompute every audit hash and check the chain links.

    Returns:
        AuditIntegrityReport: Detected tampering, if any.
    """
    broken_rows: list[UUID] = []
    broken_links: list[UUID] = []
    previous_hash_hex: str | None = None
    rows = AuditLog.objects.order_by("occurred_at", "id").only(
        "id", "action", "entity_type", "entity_id", "entity_app_label", "origin",
        "ip_address", "user_agent", "changes", "metadata", "occurred_at",
        "previous_hash", "row_hash", "actor_user_id", "actor_external_invitation_id",
        "actor_role", "actor_roles", "category",
    )
    checked = 0
    for row in rows:
        checked += 1
        expected_link = bytes.fromhex(previous_hash_hex) if previous_hash_hex else None
        stored_link = bytes(row.previous_hash) if row.previous_hash else None
        if stored_link != expected_link:
            broken_links.append(row.pk)
        payload = _row_payload(row, previous_hash_hex)
        expected_hash = hashlib.sha256(json.dumps(payload.as_dict(), sort_keys=True, default=str).encode("utf-8")).digest()
        if bytes(row.row_hash) != expected_hash:
            broken_rows.append(row.pk)
        # Continua com o hash armazenado para não propagar uma única falha a todas as linhas seguintes.
        previous_hash_hex = bytes(row.row_hash).hex()
    return AuditIntegrityReport(checked=checked, broken_rows=broken_rows, broken_links=broken_links)
