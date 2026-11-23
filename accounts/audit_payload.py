"""
Canonical data used to compute the SHA-256 hash chain of ``audit_log``.

The fields are exactly those included in the hash. Changing the structure breaks
the chain of already stored rows, so optional keys (``actor_roles`` and
``category``) are included only when set, preserving compatibility with old rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class AuditPayload:
    """
    Canonical fields of an audit entry.

    Attributes:
        action: Audit action code.
        entity_type: Affected entity type.
        entity_id: Affected entity identifier.
        entity_app_label: Django app label of the affected entity.
        origin: Network origin of the request.
        ip_address: Client IP address.
        user_agent: Client user agent.
        changes: Field changes recorded by the operation.
        metadata: Extra context recorded by the operation.
        occurred_at: Moment of the operation.
        previous_hash_hex: Hexadecimal hash of the previous row.
        actor_user_id: Institutional actor identifier, when present.
        actor_external_invitation_id: External actor identifier, when present.
        actor_role: Primary role code of the actor.
        actor_roles: Every role code held by the actor.
        category: Granular verbosity category of the entry.
    """

    action: str
    entity_type: str
    entity_id: str
    entity_app_label: str
    origin: str
    ip_address: str | None
    user_agent: str
    changes: dict[str, Any]
    metadata: dict[str, Any]
    occurred_at: datetime
    previous_hash_hex: str | None
    actor_user_id: str | None = None
    actor_external_invitation_id: str | None = None
    actor_role: str | None = None
    actor_roles: tuple[str, ...] = ()
    category: str = ""

    def as_dict(self) -> dict[str, Any]:
        """
        Build the canonical mapping hashed by ``record_audit``.

        Returns:
            dict[str, Any]: Payload with the fields used by the hash.
        """
        payload: dict[str, Any] = {
            "actor_user_id": self.actor_user_id,
            "actor_external_invitation_id": self.actor_external_invitation_id,
            "actor_role": self.actor_role,
            "action": self.action,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_app_label": self.entity_app_label,
            "origin": self.origin,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "changes": self.changes,
            "metadata": self.metadata,
            "occurred_at": self.occurred_at.isoformat(),
            "previous_hash": self.previous_hash_hex,
        }
        if self.actor_roles:
            payload["actor_roles"] = list(self.actor_roles)
        if self.category:
            payload["category"] = self.category
        return payload
