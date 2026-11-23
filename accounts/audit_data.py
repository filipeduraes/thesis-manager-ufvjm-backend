"""
Structured, presentation-free representation of audit records.

The API returns these objects so clients can build their own labels and
sentences. Values are raw codes, identifiers and context captured at write
time; no Portuguese prose or UI formatting is produced here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from accounts.audit_identifiers import AuditCategory, AuditField, EntityType
from accounts.models import AuditLog


@dataclass(frozen=True)
class AuditActorData:
    """Actor identity stored with the audit record."""

    id: str | None
    name: str
    email: str | None
    roles: tuple[str, ...]
    is_external: bool


@dataclass(frozen=True)
class AuditEntityData:
    """Entity affected by the audited action."""

    type: EntityType
    id: str
    name: str
    reference: str
    exists: bool = True


@dataclass(frozen=True)
class AuditEventData:
    """Complete structured payload returned by the audit API."""

    id: str
    timestamp: datetime
    action: str
    category: AuditCategory
    entity: AuditEntityData
    actor: AuditActorData
    origin: str
    ip_address: str | None
    metadata: dict[str, Any]
    changes: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Serialize the event using stable JSON-friendly values."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "category": self.category.value,
            "entity": {
                "type": self.entity.type.value,
                "id": self.entity.id,
                "name": self.entity.name,
                "reference": self.entity.reference,
                "exists": self.entity.exists,
            },
            "actor": {
                "id": self.actor.id,
                "name": self.actor.name,
                "email": self.actor.email,
                "roles": list(self.actor.roles),
                "isExternal": self.actor.is_external,
            },
            "origin": self.origin,
            "ipAddress": self.ip_address,
            "metadata": self.metadata,
            "changes": self.changes,
        }


def typed_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize known metadata keys into a stable, typed view.

    Unknown keys are preserved so historical records remain readable.

    Args:
        metadata: Raw metadata stored in the audit row.

    Returns:
        dict[str, Any]: Mapping keyed by canonical field identifiers.
    """
    result: dict[str, Any] = dict(metadata)
    for field in AuditField:
        value = result.get(field.value)
        if value is not None:
            result[field.value] = value
    return result


def resolve_audit_events(logs: Iterable[AuditLog]) -> list[AuditEventData]:
    """
    Resolve related records for a batch of audit rows in bounded queries.

    Args:
        logs: Audit rows to convert.

    Returns:
        list[AuditEventData]: Structured events in the same order as ``logs``.
    """
    from academic.models import Course, Department
    from accounts.models import ExternalInvitation, User
    from theses.models import Defense, Thesis, ThesisDocument, ThesisFile

    records = list(logs)
    user_ids = {str(log.actor_user_id) for log in records if log.actor_user_id}
    invitation_ids = {str(log.actor_external_invitation_id) for log in records if log.actor_external_invitation_id}
    thesis_ids: set[str] = set()
    defense_ids: set[str] = set()
    file_ids: set[str] = set()
    document_ids: set[str] = set()
    course_ids: set[str] = set()
    department_ids: set[str] = set()

    for log in records:
        entity_id = log.entity_id
        entity = EntityType.parse(log.entity_type)
        if entity == EntityType.USER and entity_id:
            user_ids.add(entity_id)
        elif entity == EntityType.EXTERNAL_INVITATION and entity_id:
            invitation_ids.add(entity_id)
        elif entity == EntityType.COURSE and entity_id:
            course_ids.add(entity_id)
        elif entity == EntityType.DEPARTMENT and entity_id:
            department_ids.add(entity_id)
        elif entity == EntityType.THESIS and entity_id:
            thesis_ids.add(entity_id)
        elif entity == EntityType.THESIS_FILE and entity_id:
            file_ids.add(entity_id)
        elif entity == EntityType.THESIS_DOCUMENT and entity_id:
            document_ids.add(entity_id)
        elif entity == EntityType.DEFENSE and entity_id:
            defense_ids.add(entity_id)

    users = {
        str(user.pk): user
        for user in User.objects.filter(pk__in=user_ids).prefetch_related("profile_roles")
    }
    invitations = {str(item.pk): item for item in ExternalInvitation.objects.filter(pk__in=invitation_ids)}
    courses = {str(item.pk): item for item in Course.objects.filter(pk__in=course_ids)}
    departments = {str(item.pk): item for item in Department.objects.filter(pk__in=department_ids)}
    theses = {str(item.pk): item for item in Thesis.objects.filter(pk__in=thesis_ids)}
    files = {str(item.pk): item for item in ThesisFile.objects.filter(pk__in=file_ids)}
    documents = {str(item.pk): item for item in ThesisDocument.objects.filter(pk__in=document_ids)}
    defenses = {str(item.pk): item for item in Defense.objects.filter(pk__in=defense_ids)}

    def thesis_for(log: AuditLog) -> Thesis | None:
        entity = EntityType.parse(log.entity_type)
        if entity == EntityType.THESIS:
            return theses.get(log.entity_id)
        if entity == EntityType.THESIS_FILE:
            thesis_file = files.get(log.entity_id)
            return theses.get(str(thesis_file.thesis_id)) if thesis_file else None
        if entity == EntityType.THESIS_DOCUMENT:
            document = documents.get(log.entity_id)
            return theses.get(str(document.thesis_id)) if document else None
        if entity == EntityType.DEFENSE:
            defense = defenses.get(log.entity_id)
            return theses.get(str(defense.thesis_id)) if defense else None
        return None

    events: list[AuditEventData] = []
    for log in records:
        actor_user = users.get(str(log.actor_user_id)) if log.actor_user_id else None
        invitation = invitations.get(str(log.actor_external_invitation_id)) if log.actor_external_invitation_id else None
        assigned = set()
        if actor_user is not None:
            assigned = {
                role.role for role in actor_user.profile_roles.all() if role.revoked_at is None
            }
        roles = tuple(
            code for code in ("ADMIN", "COORDINATOR", "ADVISOR", "STUDENT") if code in assigned
        )
        actor = AuditActorData(
            id=str(actor_user.pk) if actor_user else (str(invitation.pk) if invitation else None),
            name=(actor_user.name or actor_user.email) if actor_user else (invitation.name if invitation else "Sistema"),
            email=actor_user.email if actor_user else (invitation.email if invitation else None),
            roles=roles,
            is_external=invitation is not None,
        )
        entity = (EntityType.parse(log.entity_type) or EntityType.AUDIT_LOG)
        name = ""
        reference = ""
        if entity == EntityType.USER and actor_user:
            name, reference = actor_user.name or actor_user.email, actor_user.email
        elif entity == EntityType.EXTERNAL_INVITATION and invitation:
            name, reference = invitation.name, invitation.email
        elif entity == EntityType.COURSE and log.entity_id in courses:
            course = courses[log.entity_id]
            name, reference = course.name, course.code
        elif entity == EntityType.DEPARTMENT and log.entity_id in departments:
            name = departments[log.entity_id].name
        elif (thesis := thesis_for(log)) is not None:
            name, reference = thesis.title, thesis.reference_code
        elif entity == EntityType.REPORT_OVERVIEW:
            name = "Relatório geral"

        if entity == EntityType.THESIS_FILE and reference.isdigit():
            reference = ""
        exists = True
        if entity in {EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT, EntityType.DEFENSE}:
            exists = thesis_for(log) is not None
        elif entity == EntityType.COURSE:
            exists = log.entity_id in courses
        elif entity == EntityType.DEPARTMENT:
            exists = log.entity_id in departments
        elif entity == EntityType.USER:
            exists = log.entity_id in users
        elif entity == EntityType.EXTERNAL_INVITATION:
            exists = log.entity_id in invitations

        name = name or (log.entity_id if not exists else "")
        reference = reference or (log.entity_id if not exists else "")

        events.append(
            AuditEventData(
                id=str(log.pk),
                timestamp=log.occurred_at,
                action=log.action,
                category=AuditCategory(log.category) if log.category else AuditCategory.ACCESS,
                entity=AuditEntityData(type=entity, id=log.entity_id, name=name, reference=reference, exists=exists),
                actor=actor,
                origin=log.origin,
                ip_address=str(log.ip_address) if log.ip_address else None,
                metadata=typed_metadata(log.metadata if isinstance(log.metadata, dict) else {}),
                changes=dict(log.changes) if isinstance(log.changes, dict) else {},
            )
        )
    return events
