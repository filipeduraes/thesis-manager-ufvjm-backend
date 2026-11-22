"""
Audit helpers for defense operations.

Centralizes the typed ``entity_type=EntityType.DEFENSE`` identifier that was
previously repeated as a raw string in every committee event. The app label is
derived from the entity type, and stored metadata is unchanged.
"""
from __future__ import annotations

from django.http import HttpRequest

from accounts.audit_identifiers import EntityType
from accounts.audit_entry import AuditEntry
from accounts.models import AuditLog, ExternalInvitation, User
from accounts.services import record_audit
from theses.models import Defense


def audit_defense(action: str, defense: Defense, *, request: HttpRequest | None = None, actor_user: User | None = None, actor_external_invitation: ExternalInvitation | None = None, metadata: dict | None = None) -> AuditLog:
    """
    Append an audit entry associated with a defense session.

    Args:
        action: Audit action code.
        defense: Defense session the entry refers to.
        request: HTTP request used for audit context.
        actor_user: Institutional actor, when present.
        actor_external_invitation: External actor, when present.
        metadata: Additional context of the operation.

    Returns:
        AuditLog: The appended audit entry.
    """
    return record_audit(AuditEntry(
        action=action,
        entity_type=EntityType.DEFENSE,
        entity_id=str(defense.pk),
        request=request,
        actor_user=actor_user,
        actor_external_invitation=actor_external_invitation,
        metadata=metadata or {},
    ))
