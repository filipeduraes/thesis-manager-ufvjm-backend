"""
CSV/JSON export for audit records.

This layer consumes raw ``AuditLog`` rows and formats them specifically for
spreadsheets. It deliberately does not use ``AuditPresenter``: export cells have
their own conventions and must not depend on UI sentences.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from accounts.audit_data import AuditEventData
from accounts.audit_identifiers import CATEGORY_LABELS, ENTITY_TYPE_LABELS, AuditCategory
from accounts.models import AuditAction, AuditLog
from accounts.services import role_label


def _json(value: Any) -> str:
    """Serialize a JSON field deterministically for a spreadsheet cell."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def build_export_row(event: AuditEventData, actor_name: str, roles: Iterable[str], hash_hex: str = "") -> dict[str, Any]:
    """
    Build one CSV/JSON row from a structured audit event.

    Args:
        event: Structured event data.
        actor_name: Display name of the actor.
        roles: Active role codes of the actor.
        hash_hex: Short hexadecimal row hash.

    Returns:
        dict[str, Any]: Row keyed by the public serializer field names.
    """
    role_codes = list(roles)
    return {
        "id": event.id,
        "timestamp": event.timestamp.isoformat(),
        "actor": actor_name,
        "actorEmail": event.actor.email or "",
        "role": "; ".join(role_label(code) or code for code in role_codes),
        "roles": "; ".join(role_label(code) or code for code in role_codes),
        "roleCodes": "; ".join(role_codes),
        "action": AuditAction(event.action).label,
        "event": event.as_dict(),
        "actionCode": event.action,
        "actionLabel": AuditAction(event.action).label,
        "summary": f"{AuditAction(event.action).label}: {event.entity.name}".strip(),
        "verbosity": "TRACE" if event.category == AuditCategory.ACCESS else "RELEVANT",
        "verbosityLabel": event.category.label,
        "category": event.category.value,
        "categoryLabel": CATEGORY_LABELS[event.category],
        "entityType": event.entity.type.value,
        "entityTypeLabel": ENTITY_TYPE_LABELS[event.entity.type],
        "entityAppLabel": event.entity.type.app_label,
        "entityId": event.entity.id,
        "entityName": event.entity.name,
        "entityNameShort": event.entity.name,
        "entityReference": event.entity.reference,
        "details": _json(event.metadata),
        "origin": event.origin,
        "originCode": event.origin,
        "ipAddress": event.ip_address or "",
        "channel": "Token externo" if event.actor.is_external else "Sistema web",
        "hash": hash_hex,
        "changes": _json(event.changes),
        "metadata": _json(event.metadata),
    }
