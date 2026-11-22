"""
Typed payload contracts for audit events.

Each event has a fixed set of fields. Writers build payloads through
``event_payload`` so misspelled or unexpected keys fail immediately instead of
silently producing unreadable audit rows. The persisted shape is versioned so
future format changes are explicit.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from accounts.audit_identifiers import AuditEvent

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class EventPayload:
    """Base contract shared by every audit event."""

    event: AuditEvent

    def as_dict(self) -> dict[str, Any]:
        """Return the deterministic mapping stored in ``metadata``."""
        values: dict[str, Any] = {
            "event": self.event.value,
            "schemaVersion": SCHEMA_VERSION,
        }
        aliases = {"fromStatus": "from", "toStatus": "to"}
        for field in fields(self):
            if field.name == "event":
                continue
            value = getattr(self, field.name)
            if value is not None:
                values[aliases.get(field.name, field.name)] = value
        return values


@dataclass(frozen=True)
class LoginPayload(EventPayload):
    sessionKey: str | None = None
    sessionExpiresAt: str | None = None
    provider: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class UploadPayload(EventPayload):
    versionNumber: int | None = None
    sha256: str | None = None
    sizeBytes: int | None = None
    documentType: str | None = None
    documentId: str | None = None
    fileName: str | None = None


@dataclass(frozen=True)
class ThesisLifecyclePayload(EventPayload):
    versionNumber: int | None = None
    targetStatus: str | None = None
    removedVersions: int | None = None
    keptVersion: int | None = None
    result: str | None = None
    referenceCode: str | None = None


@dataclass(frozen=True)
class DefenseLifecyclePayload(EventPayload):
    scheduledDatetime: str | None = None
    availabilityReset: bool | None = None
    reason: str | None = None
    result: str | None = None
    thesisNewStatus: str | None = None
    trigger: str | None = None
    fromStatus: str | None = None
    toStatus: str | None = None


@dataclass(frozen=True)
class CommitteePayload(EventPayload):
    memberId: str | None = None
    memberName: str | None = None
    memberRole: str | None = None
    type: str | None = None
    invitationId: str | None = None
    previousInvitationId: str | None = None
    fromStatus: str | None = None
    toStatus: str | None = None


@dataclass(frozen=True)
class EvaluationPayload(EventPayload):
    memberId: str | None = None
    decision: str | None = None
    score: str | None = None


@dataclass(frozen=True)
class GenericEventPayload(EventPayload):
    """Fallback for events that only carry the event code."""


PAYLOAD_TYPES: dict[AuditEvent, type[EventPayload]] = {
    AuditEvent.SUBMITTED: ThesisLifecyclePayload,
    AuditEvent.SUBMISSION_WITHDRAWN: ThesisLifecyclePayload,
    AuditEvent.VERSION_HISTORY_PURGED: ThesisLifecyclePayload,
    AuditEvent.DEFENSE_RESULT: ThesisLifecyclePayload,
    AuditEvent.PUBLISHED: ThesisLifecyclePayload,
    AuditEvent.DOCUMENT_VALIDATED: UploadPayload,
    AuditEvent.DEFENSE_SCHEDULED: DefenseLifecyclePayload,
    AuditEvent.AUTO_STARTED_BY_SCHEDULE: DefenseLifecyclePayload,
    AuditEvent.DEFENSE_UPDATED: DefenseLifecyclePayload,
    AuditEvent.DEFENSE_CANCELLED: DefenseLifecyclePayload,
    AuditEvent.DEFENSE_STARTED: DefenseLifecyclePayload,
    AuditEvent.DEFENSE_CLOSED: DefenseLifecyclePayload,
    AuditEvent.AUTO_CLOSE_FAILED: DefenseLifecyclePayload,
    AuditEvent.STATUS_REVERTED_BY_ADMIN: DefenseLifecyclePayload,
    AuditEvent.MEMBER_ADDED: CommitteePayload,
    AuditEvent.MEMBER_REMOVED: CommitteePayload,
    AuditEvent.AVAILABILITY_RESPONDED: CommitteePayload,
    AuditEvent.EXTERNAL_TOKEN_REGENERATED: CommitteePayload,
    AuditEvent.EVALUATION_SUBMITTED: EvaluationPayload,
    AuditEvent.EVALUATION_BLOCKED_AFTER_CLOSE: EvaluationPayload,
}


def event_payload(event: AuditEvent, **values: Any) -> dict[str, Any]:
    """
    Build a validated metadata mapping for an audit event.

    Args:
        event: Event being recorded.
        **values: Event-specific field values.

    Returns:
        dict[str, Any]: Deterministic metadata mapping.

    Raises:
        ValueError: When a field is not part of the event contract.
    """
    payload_type = PAYLOAD_TYPES.get(event, GenericEventPayload)
    aliases = {"from": "fromStatus", "to": "toStatus"}
    normalized = {aliases.get(key, key): value for key, value in values.items()}
    allowed = {field.name for field in fields(payload_type) if field.name != "event"}
    unknown = set(normalized) - allowed
    if unknown:
        raise ValueError(f"Campos inesperados no evento {event.value}: {sorted(unknown)}")
    return payload_type(event=event, **normalized).as_dict()
