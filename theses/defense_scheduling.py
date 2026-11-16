"""
Scheduling and lifecycle services for thesis defense sessions.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import URLValidator
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from accounts.models import AuditAction, AuditLog, User
from accounts.services import record_audit
from theses.defense_committee import _can_manage_defense, check_committee_schedule_conflicts, defense_member_payload, pending_availability_members, reset_committee_availability
from theses.models import ACTIVE_DEFENSE_STATUSES, APPROVED_DEFENSE_STATUSES, SCHEDULABLE_STATUSES, CommitteeRole, Defense, DefenseMemberEvaluation, DefenseModality, DefenseResult, DefenseStatus, DefenseStatusHistory, Thesis, ThesisStatus, ThesisStatusHistory
from theses.services import purge_thesis_versions, user_is_admin

logger = logging.getLogger(__name__)

AUTO_CLOSE_REASON = "Resultado calculado automaticamente após a última avaliação."
REMOTE_LOCATION_ERROR = "Para defesa remota, informe o link da reunião (URL http ou https)."
MEETING_URL_VALIDATOR = URLValidator(schemes=["http", "https"])


def validate_defense_location(modality: str, location: str) -> None:
    """
    Validate the defense location for its modality.

    Remote defenses need a meeting link (http/https URL); in-person defenses keep
    free-text locations.

    Args:
        modality: DefenseModality code.
        location: Stripped location value.

    Raises:
        ValidationError: When a remote defense has no valid http(s) link (key ``location``).
    """
    if modality != DefenseModality.REMOTE:
        return
    if not location:
        raise ValidationError({"location": REMOTE_LOCATION_ERROR})
    try:
        MEETING_URL_VALIDATOR(location)
    except ValidationError as exc:
        raise ValidationError({"location": REMOTE_LOCATION_ERROR}) from exc


def record_defense_status_change(defense: Defense, previous_status: str, changed_by: User | None, reason: str) -> DefenseStatusHistory:
    """
    Append a status transition of a defense to its history.

    Must be called inside the transaction that changes ``defense.status`` and after
    the new status has been assigned, so history and status are committed together.

    Args:
        defense: Defense whose ``status`` already holds the new value.
        previous_status: Status before the transition (empty string on creation).
        changed_by: User responsible, or None for automatic transitions.
        reason: Human-readable explanation stored with the entry.

    Returns:
        DefenseStatusHistory: Created history entry.
    """
    return DefenseStatusHistory.objects.create(defense=defense, previous_status=previous_status, new_status=defense.status, changed_by=changed_by, reason=reason)


def defense_status_history_payload(defense: Defense) -> list[dict[str, Any]]:
    """
    Serialize the status history of a defense in chronological order.

    Args:
        defense: Defense session.

    Returns:
        list[dict[str, Any]]: History entries, oldest first.
    """
    entries = defense.status_history.select_related("changed_by").order_by("created_at")
    history: list[dict[str, Any]] = []
    for entry in entries:
        previous_label = DefenseStatus(entry.previous_status).label if entry.previous_status else None
        changed_by_name = (entry.changed_by.name or entry.changed_by.email) if entry.changed_by else None
        history.append({
            "previousStatus": entry.previous_status or None,
            "previousStatusLabel": previous_label,
            "newStatus": entry.new_status,
            "newStatusLabel": DefenseStatus(entry.new_status).label,
            "changedBy": changed_by_name,
            "reason": entry.reason,
            "createdAt": entry.created_at.isoformat(),
        })
    return history


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
        ValidationError: When the thesis status or active defense constraint is violated,
            or a remote defense has no valid http(s) meeting link.
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
    clean_location = location.strip()
    validate_defense_location(modality, clean_location)

    defense = Defense.objects.create(
        thesis=thesis,
        scheduled_datetime=scheduled_datetime,
        duration_minutes=duration_minutes,
        modality=modality,
        location=clean_location,
        notes=notes.strip(),
        status=DefenseStatus.SCHEDULED,
        created_by=actor,
    )
    record_defense_status_change(defense, "", actor, "Banca agendada.")

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


def _transition_defense_to_in_progress(defense: Defense, thesis: Thesis, *, changed_by: User | None, reason: str) -> None:
    """
    Apply the SCHEDULED → IN_PROGRESS transition in-place for defense and thesis.

    Both objects must already be locked with select_for_update. Saves both records
    and creates ThesisStatusHistory and DefenseStatusHistory entries. Does NOT write
    an audit log entry; the caller is responsible for that.

    Args:
        defense: Locked Defense instance (status must be SCHEDULED).
        thesis: Locked Thesis instance belonging to the defense.
        changed_by: User who triggered the transition, or None for system actions.
        reason: Text stored in both ThesisStatusHistory and DefenseStatusHistory.
    """
    previous_thesis_status = thesis.status
    thesis.status = ThesisStatus.UNDER_EVALUATION
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=ThesisStatus.UNDER_EVALUATION,
        changed_by=changed_by,
        reason=reason,
        metadata={"defenseId": str(defense.pk)},
    )
    previous_defense_status = defense.status
    defense.status = DefenseStatus.IN_PROGRESS
    defense.auto_start_suspended = False
    defense.save(update_fields=["status", "auto_start_suspended", "updated_at"])
    record_defense_status_change(defense, previous_defense_status, changed_by, reason)


@transaction.atomic
def sync_defense_status(defense: Defense, *, request: HttpRequest | None = None) -> Defense:
    """
    Idempotently transition SCHEDULED → IN_PROGRESS when scheduled_datetime has passed.

    Intended to be called at the beginning of endpoints that read or mutate a defense
    so that the status stays current without a background job. If auto_start_suspended
    is True the transition is skipped (set by revert_defense_status to prevent
    immediate re-opening after a manual revert). No-op for any status other than
    SCHEDULED.

    The same availability rule as ``start_defense`` applies: while a non-alternate
    member has not confirmed availability, the defense stays SCHEDULED without
    raising, so read endpoints keep working and the detail reports who is missing.

    Args:
        defense: Defense session (may be stale — a fresh copy is fetched under lock).
        request: HTTP request used for audit context.

    Returns:
        Defense: Refreshed defense instance (status may have changed to IN_PROGRESS).
    """
    if defense.status != DefenseStatus.SCHEDULED or defense.auto_start_suspended or timezone.now() < defense.scheduled_datetime:
        return defense
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED or defense.auto_start_suspended:
        return defense
    if pending_availability_members(defense):
        # DEBUG porque a leitura pode repetir esta checagem a cada requisição até todos confirmarem.
        logger.debug("Auto-start skipped for defense %s: pending availability confirmations", defense.pk)
        return defense
    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    _transition_defense_to_in_progress(defense, thesis, changed_by=None, reason="Banca iniciada automaticamente pelo horário agendado.")
    record_audit(
        action=AuditAction.UPDATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=None,
        metadata={"event": "AUTO_STARTED_BY_SCHEDULE"},
    )
    return defense


@transaction.atomic
def update_defense(defense: Defense, *, scheduled_datetime: datetime | None = None, duration_minutes: int | None = None, modality: str | None = None, location: str | None = None, notes: str | None = None, actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Update scheduling details for a not-yet-started defense.

    When the date, time or duration actually changes (a value different from the
    current one), every member's availability answer is reset to PENDING, because
    the previous answers referred to another slot. The remote meeting link is
    validated only when modality or location change, so older defenses with a
    non-conforming location stay editable in their other fields.

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
        ValidationError: When the defense is not in SCHEDULED status, the modality is
            invalid, or a new date, time or duration overlaps another active defense of
            an institutional committee member, or a changed modality/location leaves a
            remote defense without a valid http(s) meeting link.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível alterar uma banca agendada (antes de iniciar).")

    datetime_changed = scheduled_datetime is not None and scheduled_datetime != defense.scheduled_datetime
    duration_changed = duration_minutes is not None and duration_minutes != defense.duration_minutes
    modality_changed = modality is not None and modality != defense.modality
    location_changed = location is not None and location.strip() != defense.location
    extra_fields: list[str] = []
    if scheduled_datetime is not None:
        defense.scheduled_datetime = scheduled_datetime
        defense.auto_start_suspended = False
        extra_fields.append("auto_start_suspended")
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
    # Só valida o local quando modalidade ou local mudam, para não travar bancas antigas fora do padrão.
    if modality_changed or location_changed:
        validate_defense_location(defense.modality, defense.location)
    if scheduled_datetime is not None or duration_minutes is not None:
        check_committee_schedule_conflicts(defense)
    defense.save(update_fields=["scheduled_datetime", "duration_minutes", "modality", "location", "notes", "updated_at"] + extra_fields)
    availability_reset = datetime_changed or duration_changed
    if availability_reset:
        reset_committee_availability(defense)

    record_audit(
        action=AuditAction.UPDATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "DEFENSE_UPDATED", "availabilityReset": availability_reset},
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

    defense.previous_status = defense.status
    defense.status = DefenseStatus.CANCELLED
    defense.auto_start_suspended = False
    defense.save(update_fields=["status", "previous_status", "auto_start_suspended", "updated_at"])
    record_defense_status_change(defense, defense.previous_status, actor, cancel_reason)

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

    Allowed for administrators, coordinators and the advisor of the thesis
    (``_can_manage_defense``), following the review decision to keep the advisor
    in control of their own defense.

    Args:
        defense: Defense session to start.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (IN_PROGRESS) defense record.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When the defense is not in SCHEDULED status or a non-alternate
            member has not confirmed availability (the message lists who is missing).
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível iniciar uma banca agendada.")
    pending_members = pending_availability_members(defense)
    if pending_members:
        pending_names = ", ".join(member.name for member in pending_members)
        raise ValidationError(f"Confirmação de disponibilidade pendente: {pending_names}.")

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    _transition_defense_to_in_progress(defense, thesis, changed_by=actor, reason="Banca iniciada.")

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

    Allowed for administrators, coordinators and the advisor of the thesis
    (``_can_manage_defense``), following the review decision to keep the advisor
    in control of their own defense.

    Args:
        defense: Defense session to close.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (COMPLETED) defense record.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When the defense is not IN_PROGRESS or evaluations are missing.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    return _finalize_defense(defense, closed_by=actor, automatic=False, request=request)


def all_required_evaluations_submitted(defense: Defense) -> bool:
    """
    Return whether every non-alternate member of the defense has an evaluation.

    Args:
        defense: Defense session.

    Returns:
        bool: True when there is at least one non-alternate member and all evaluated.
    """
    non_alternate_ids = list(defense.members.exclude(role=CommitteeRole.ALTERNATE).values_list("id", flat=True))
    if not non_alternate_ids:
        return False
    evaluated_count = DefenseMemberEvaluation.objects.filter(member__in=non_alternate_ids).count()
    return evaluated_count == len(non_alternate_ids)


@transaction.atomic
def _finalize_defense(defense: Defense, *, closed_by: User | None, automatic: bool, request: HttpRequest | None = None) -> Defense:
    """
    Compute the committee result, complete the defense and update the thesis.

    Shared by the manual ``close_defense`` and the automatic close triggered after
    the last evaluation, so both apply the same quorum, result and purge rules.
    Permission checks are the caller's responsibility.

    Args:
        defense: Defense session to close.
        closed_by: User who closed it, or None for the automatic close.
        automatic: Whether the close was triggered by the last evaluation.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated (COMPLETED) defense record.

    Raises:
        ValidationError: When the defense is not IN_PROGRESS, evaluations are missing,
            or the thesis version purge fails (e.g. approval without a current PDF).
    """
    defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if defense.status != DefenseStatus.IN_PROGRESS:
        raise ValidationError("Só é possível encerrar uma banca em andamento.")

    # Verificar quorum: todos os membros não-suplentes precisam ter avaliado
    non_alternate_ids = list(defense.members.exclude(role=CommitteeRole.ALTERNATE).values_list("id", flat=True))
    if not non_alternate_ids:
        raise ValidationError("A banca não possui membros avaliadores (não-suplentes).")
    if not all_required_evaluations_submitted(defense):
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
    result_reason = f"Banca encerrada. Resultado: {DefenseResult(result).label}."
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=new_thesis_status,
        changed_by=closed_by,
        reason=result_reason,
        metadata={"defenseId": str(defense.pk), "defenseResult": result, "automatic": automatic},
    )

    defense.status = DefenseStatus.COMPLETED
    defense.save(update_fields=["status", "updated_at"])
    history_reason = AUTO_CLOSE_REASON if automatic else result_reason
    record_defense_status_change(defense, DefenseStatus.IN_PROGRESS, closed_by, history_reason)

    if new_thesis_status in APPROVED_DEFENSE_STATUSES:
        purge_thesis_versions(thesis, actor=closed_by, request=request)

    record_audit(
        action=AuditAction.EVALUATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=closed_by,
        metadata={"event": "DEFENSE_CLOSED", "result": result, "thesisNewStatus": new_thesis_status, "trigger": "AUTO_AFTER_LAST_EVALUATION" if automatic else "MANUAL"},
    )
    return defense


def auto_close_after_last_evaluation(defense: Defense, request: HttpRequest | None = None) -> str | None:
    """
    Close the defense automatically when every required evaluation is in.

    Runs in its own savepoint: if closing raises ValidationError (for example, an
    approval whose thesis has no current PDF), only the close is rolled back, the
    evaluation that triggered it stays saved and the defense stays IN_PROGRESS so
    it can still be closed manually. A concurrent close is not reported as error.

    Args:
        defense: Defense that just received an evaluation.
        request: HTTP request used for audit context.

    Returns:
        str | None: Error message when the automatic close failed, otherwise None.
    """
    try:
        with transaction.atomic():
            locked_defense = Defense.objects.select_for_update().get(pk=defense.pk)
            if locked_defense.status != DefenseStatus.IN_PROGRESS or not all_required_evaluations_submitted(locked_defense):
                return None
            _finalize_defense(locked_defense, closed_by=None, automatic=True, request=request)
    except ValidationError as exc:
        error_message = exc.messages[0] if exc.messages else "Não foi possível encerrar a banca automaticamente."
        logger.warning("Automatic close failed for defense %s after last evaluation: %s", defense.pk, error_message)
        return error_message
    logger.info("Defense %s closed automatically after last evaluation", defense.pk)
    return None


@transaction.atomic
def revert_defense_status(defense: Defense, *, reason: str, actor: User, request: HttpRequest | None = None) -> Defense:
    """
    Revert a defense to its previous status (admin only).

    Allowed transitions:
    - IN_PROGRESS → SCHEDULED (also reverts thesis from UNDER_EVALUATION to DEFENSE_SCHEDULED).
    - CANCELLED → previous status stored in defense.previous_status, defaulting to SCHEDULED.

    After reverting to SCHEDULED, auto_start_suspended is set to True so that
    sync_defense_status does not immediately re-open the defense if the scheduled
    time has already passed. The flag is cleared when the defense datetime is
    updated via update_defense or when it is manually started via start_defense.

    COMPLETED defenses cannot be reverted.
    Existing evaluations are preserved.

    Args:
        defense: Defense session to revert.
        reason: Mandatory explanation for the revert (non-empty string required).
        actor: Administrator performing the action.
        request: HTTP request used for audit context.

    Returns:
        Defense: Updated defense record.

    Raises:
        PermissionDenied: When the actor is not an administrator.
        ValidationError: When reason is empty, the status is COMPLETED, or the
            current status is not revertible.
    """
    if not user_is_admin(actor):
        raise PermissionDenied()
    if not reason or not reason.strip():
        raise ValidationError({"reason": "Este campo é obrigatório."})

    defense = Defense.objects.select_for_update().get(pk=defense.pk)

    if defense.status == DefenseStatus.COMPLETED:
        raise ValidationError("Não é possível reverter uma banca já encerrada.")
    if defense.status not in (DefenseStatus.IN_PROGRESS, DefenseStatus.CANCELLED):
        raise ValidationError("Só é possível reverter bancas em andamento ou canceladas.")

    from_status = defense.status
    to_status = defense.previous_status or DefenseStatus.SCHEDULED

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    previous_thesis_status = thesis.status
    thesis.status = ThesisStatus.DEFENSE_SCHEDULED
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=ThesisStatus.DEFENSE_SCHEDULED,
        changed_by=actor,
        reason=f"Status da banca revertido pelo administrador. {reason}".strip(),
        metadata={"defenseId": str(defense.pk)},
    )

    defense.status = to_status
    defense.previous_status = ""
    defense.auto_start_suspended = True
    defense.save(update_fields=["status", "previous_status", "auto_start_suspended", "updated_at"])
    record_defense_status_change(defense, from_status, actor, f"Status revertido pelo administrador. {reason.strip()}")

    record_audit(
        action=AuditAction.UPDATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "STATUS_REVERTED_BY_ADMIN", "from": from_status, "to": to_status, "reason": reason},
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
