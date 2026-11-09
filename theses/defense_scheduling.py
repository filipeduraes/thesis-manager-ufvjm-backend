"""
Scheduling and lifecycle services for thesis defense sessions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest

from accounts.models import AuditAction, User
from accounts.services import record_audit
from theses.defense_committee import _can_manage_defense, defense_member_payload
from theses.models import ACTIVE_DEFENSE_STATUSES, APPROVED_DEFENSE_STATUSES, SCHEDULABLE_STATUSES, CommitteeRole, Defense, DefenseMemberEvaluation, DefenseModality, DefenseResult, DefenseStatus, Thesis, ThesisStatus, ThesisStatusHistory
from theses.services import purge_thesis_versions, user_is_admin, user_is_coordinator


@transaction.atomic
def schedule_defense(thesis: Thesis, *, scheduled_datetime: datetime, duration_minutes: int = 120, modality: str = DefenseModality.IN_PERSON, location: str = "", notes: str = "", actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Schedule a defense session for an advisor-approved thesis.

    Args:
        thesis: Thesis to defend.
        scheduled_datetime: Date and time of the defense.
        duration_minutes: Expected duration in minutes (default 120).
        modality: DefenseModality code.
        location: Physical or virtual meeting location.
        notes: Additional scheduling notes.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Created defense record.

    Raises:
        PermissionDenied: When the actor is not authorized to schedule this defense.
        ValidationError: When the thesis status or active defense constraint is violated.
    """
    if not _can_manage_defense(actor, thesis.pk):
        raise PermissionDenied()

    thesis = Thesis.objects.select_for_update().get(pk=thesis.pk)
    if thesis.status not in SCHEDULABLE_STATUSES:
        raise ValidationError("A monografia precisa estar aprovada pelo orientador ou pronta para banca para agendar uma defesa.")
    if Defense.objects.filter(thesis=thesis, status__in=ACTIVE_DEFENSE_STATUSES).exists():
        raise ValidationError("Esta monografia já possui uma defesa ativa (agendada ou em andamento).")
    if modality not in DefenseModality.values:
        raise ValidationError("Modalidade de defesa inválida.")

    defense = Defense.objects.create(
        thesis=thesis,
        scheduled_datetime=scheduled_datetime,
        duration_minutes=duration_minutes,
        modality=modality,
        location=location.strip(),
        notes=notes.strip(),
        status=DefenseStatus.SCHEDULED,
        created_by=actor,
    )

    previous_status = thesis.status
    thesis.status = ThesisStatus.DEFENSE_SCHEDULED
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_status,
        new_status=ThesisStatus.DEFENSE_SCHEDULED,
        changed_by=actor,
        reason="Defesa agendada.",
        metadata={"defenseId": str(defense.pk)},
    )
    record_audit(
        action=AuditAction.SCHEDULE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_SCHEDULED", "scheduledDatetime": scheduled_datetime.isoformat()},
    )
    return defense


@transaction.atomic
def update_defense(defense: Defense, *, scheduled_datetime: datetime | None = None, duration_minutes: int | None = None, modality: str | None = None, location: str | None = None, notes: str | None = None, actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Update scheduling details for a not-yet-started defense.

    Args:
        defense: Defense session to update.
        scheduled_datetime: New date and time, if changing.
        duration_minutes: New duration in minutes, if changing.
        modality: New DefenseModality code, if changing.
        location: New location, if changing.
        notes: New notes, if changing.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated defense record.

    Raises:
        PermissionDenied: When the actor is not authorized to update this defense.
        ValidationError: When the defense is not in SCHEDULED status.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível alterar uma banca agendada (antes de iniciar).")

    if scheduled_datetime is not None:
        defense.scheduled_datetime = scheduled_datetime
    if duration_minutes is not None:
        defense.duration_minutes = duration_minutes
    if modality is not None:
        if modality not in DefenseModality.values:
            raise ValidationError("Modalidade de defesa inválida.")
        defense.modality = modality
    if location is not None:
        defense.location = location.strip()
    if notes is not None:
        defense.notes = notes.strip()
    defense.save(update_fields=["scheduled_datetime", "duration_minutes", "modality", "location", "notes", "updated_at"])

    record_audit(
        action=AuditAction.UPDATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_UPDATED"},
    )
    return defense


@transaction.atomic
def cancel_defense(defense: Defense, *, reason: str = "", actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Cancel a scheduled defense and restore the thesis to its pre-scheduling status.

    The previous thesis status is recovered from ThesisStatusHistory. If no history
    entry exists (edge case), the thesis reverts to ADVISOR_APPROVED.

    Args:
        defense: Defense session to cancel.
        reason: Optional cancellation reason.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (CANCELLED) defense record.

    Raises:
        PermissionDenied: When the actor is not authorized to cancel this defense.
        ValidationError: When the defense is not in SCHEDULED status.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível cancelar uma banca agendada (antes de iniciar).")

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    # Restaurar ao status anterior ao agendamento
    history_entry = thesis.status_history.filter(new_status=ThesisStatus.DEFENSE_SCHEDULED).order_by("-changed_at").first()
    restore_to = (history_entry.previous_status if history_entry and history_entry.previous_status else ThesisStatus.ADVISOR_APPROVED)

    previous_thesis_status = thesis.status
    thesis.status = restore_to
    thesis.save(update_fields=["status", "updated_at"])
    cancel_reason = f"Banca cancelada. {reason}".strip() if reason else "Banca cancelada."
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=restore_to,
        changed_by=actor,
        reason=cancel_reason,
        metadata={"defenseId": str(defense.pk)},
    )

    defense.status = DefenseStatus.CANCELLED
    defense.save(update_fields=["status", "updated_at"])

    record_audit(
        action=AuditAction.SCHEDULE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_CANCELLED", "reason": reason},
    )
    return defense


@transaction.atomic
def start_defense(defense: Defense, *, actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Transition a scheduled defense to IN_PROGRESS and the thesis to UNDER_EVALUATION.

    Restricted to administrators and coordinators. The advisor permission for
    starting a defense has not been confirmed by Filipe (pending decision).

    Args:
        defense: Defense session to start.
        actor: Admin or coordinator performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (IN_PROGRESS) defense record.

    Raises:
        PermissionDenied: When the actor is not an admin or coordinator.
        ValidationError: When the defense is not in SCHEDULED status.
    """
    if not (user_is_admin(actor) or user_is_coordinator(actor)):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível iniciar uma banca agendada.")

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    previous_thesis_status = thesis.status
    thesis.status = ThesisStatus.UNDER_EVALUATION
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=ThesisStatus.UNDER_EVALUATION,
        changed_by=actor,
        reason="Banca iniciada.",
        metadata={"defenseId": str(defense.pk)},
    )

    defense.status = DefenseStatus.IN_PROGRESS
    defense.save(update_fields=["status", "updated_at"])

    record_audit(
        action=AuditAction.SCHEDULE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_STARTED"},
    )
    return defense


@transaction.atomic
def close_defense(defense: Defense, *, actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Close an in-progress defense, compute the committee result, and update the thesis.

    All non-alternate members must have submitted evaluations before closure.
    Result is computed as: any REJECTED → REJECTED; all APPROVED → APPROVED;
    otherwise APPROVED_WITH_REMARKS. When the thesis is approved, non-current
    file versions are purged.

    Restricted to administrators and coordinators. The advisor permission for
    closing a defense has not been confirmed by Filipe (pending decision).

    Args:
        defense: Defense session to close.
        actor: Admin or coordinator performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (COMPLETED) defense record.

    Raises:
        PermissionDenied: When the actor is not an admin or coordinator.
        ValidationError: When the defense is not IN_PROGRESS or evaluations are missing.
    """
    if not (user_is_admin(actor) or user_is_coordinator(actor)):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.IN_PROGRESS:
        raise ValidationError("Só é possível encerrar uma banca em andamento.")

    # Verificar quorum: todos os membros não-suplentes precisam ter avaliado
    non_alternate_ids = list(defense.members.exclude(role=CommitteeRole.ALTERNATE).values_list("id", flat=True))
    if not non_alternate_ids:
        raise ValidationError("A banca não possui membros avaliadores (não-suplentes).")
    evaluated_ids = set(DefenseMemberEvaluation.objects.filter(member__in=non_alternate_ids).values_list("member_id", flat=True))
    missing = set(non_alternate_ids) - evaluated_ids
    if missing:
        raise ValidationError("Todos os membros não-suplentes precisam ter avaliado antes do encerramento.")

    # Calcular resultado a partir das avaliações
    decisions = list(DefenseMemberEvaluation.objects.filter(member__in=non_alternate_ids).values_list("decision", flat=True))
    if DefenseResult.REJECTED in decisions:
        result = DefenseResult.REJECTED
        new_thesis_status = ThesisStatus.REJECTED
    elif all(d == DefenseResult.APPROVED for d in decisions):
        result = DefenseResult.APPROVED
        new_thesis_status = ThesisStatus.APPROVED
    else:
        result = DefenseResult.APPROVED_WITH_REMARKS
        new_thesis_status = ThesisStatus.APPROVED_WITH_REMARKS

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    previous_thesis_status = thesis.status
    thesis.status = new_thesis_status
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=new_thesis_status,
        changed_by=actor,
        reason=f"Banca encerrada. Resultado: {DefenseResult(result).label}.",
        metadata={"defenseId": str(defense.pk), "defenseResult": result},
    )

    defense.status = DefenseStatus.COMPLETED
    defense.save(update_fields=["status", "updated_at"])

    if new_thesis_status in APPROVED_DEFENSE_STATUSES:
        purge_thesis_versions(thesis, actor=actor, request=request)

    record_audit(
        action=AuditAction.EVALUATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_CLOSED", "result": result, "thesisNewStatus": new_thesis_status},
    )
    return defense


def defense_payload(defense: Defense, viewer: Any = None) -> dict[str, Any]:
    """
    Serialize a defense session with its committee members.

    Args:
        defense: Defense session.
        viewer: Authenticated user or ExternalInvitationPrincipal used to decide
            evaluation visibility per member (passed through to defense_member_payload).

    Returns:
        dict[str, Any]: Defense payload including members list.
    """
    return {
        "id": str(defense.pk),
        "thesis": str(defense.thesis_id),
        "scheduledDatetime": defense.scheduled_datetime.isoformat(),
        "durationMinutes": defense.duration_minutes,
        "modality": defense.modality,
        "modalityLabel": DefenseModality(defense.modality).label,
        "location": defense.location,
        "notes": defense.notes,
        "status": defense.status,
        "statusLabel": DefenseStatus(defense.status).label,
        "createdAt": defense.created_at.isoformat(),
        "updatedAt": defense.updated_at.isoformat(),
        "members": [defense_member_payload(m, viewer=viewer) for m in defense.members.all()],
    }
