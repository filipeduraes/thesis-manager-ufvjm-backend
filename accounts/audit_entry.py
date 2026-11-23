"""
Context for a single audit entry.

``record_audit`` takes one ``AuditEntry`` instead of a dozen named parameters.
The dataclass groups data that belongs to the same operation (affected entity,
actor, HTTP context and content) without introducing extra layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.http import HttpRequest

from accounts.audit_identifiers import EntityType
from accounts.models import ExternalInvitation, User


@dataclass(frozen=True)
class AuditEntry:
    """
    Data required to append an entry to the audit log.

    Attributes:
        action: Audit action code.
        entity_type: Typed entity kind; its app label is derived from it.
        entity_id: Affected entity identifier.
        request: HTTP request used to derive IP, user agent and origin.
        actor_user: Institutional actor, when present.
        actor_external_invitation: External actor, when present.
        actor_role: Primary role code of the actor.
        actor_roles: Every role code held by the actor.
        origin: Network origin; derived from ``request`` when omitted.
        ip_address: Client IP address; derived from ``request`` when omitted.
        user_agent: Client user agent; derived from ``request`` when omitted.
        changes: Field changes recorded by the operation.
        metadata: Additional context recorded by the operation.
    """

    action: str
    entity_type: EntityType
    entity_id: str
    request: HttpRequest | None = None
    actor_user: User | None = None
    actor_external_invitation: ExternalInvitation | None = None
    actor_role: str | None = None
    actor_roles: tuple[str, ...] | None = None
    origin: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    changes: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
