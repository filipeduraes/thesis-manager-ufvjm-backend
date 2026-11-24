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
from django.db.models import Prefetch, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from accounts.audit_events import event_payload
from accounts.audit_identifiers import AuditEvent
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction, AuditLog, User
from accounts.services import avatar_urls_for, create_external_invitation, record_audit
from theses.audit import audit_defense
from theses.defense_committee import DefenseViewerContext, ScheduleWarningError, _can_manage_defense, _check_schedule_overlap, _external_warnings, _has_external_schedule_overlap, _institutional_warnings, build_defense_viewer_context, check_committee_schedule_conflicts, defense_member_payload, defense_schedule_warnings, pending_availability_members, reset_committee_availability
from theses.events import record_defense_cancelled, record_defense_completed, record_defense_member_invited, record_defense_rescheduled, record_defense_scheduled
from theses.models import ACTIVE_DEFENSE_STATUSES, APPROVED_DEFENSE_STATUSES, SCHEDULABLE_STATUSES, CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseModality, DefenseResult, DefenseStatus, DefenseStatusHistory, Thesis, ThesisStatus, ThesisStatusHistory
from theses.services import PublicationTrigger, publish_if_ready, purge_thesis_versions, thesis_summary_payload, user_is_admin

logger = logging.getLogger(__name__)

AUTO_CLOSE_REASON = "Resultado calculado automaticamente após a última avaliação."
DEFENSE_CLOSE_ENTRIES_ATTR = "defense_close_entries"
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
    return DefenseStatusHistory.objects.create(defense=defense, previous_status=previous_status, new_status=defense.status, changed_by_id=changed_by.pk if changed_by else None, reason=reason)


def defense_status_history_payload(defense: Defense) -> list[dict[str, Any]]:
    """
    Serialize the status history of a defense in chronological order.

    Args:
        defense: Defense session.

    Returns:
        list[dict[str, Any]]: History entries, oldest first.
    """
    entries = defense.status_history.order_by("created_at")
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
def schedule_defense(thesis: Thesis, *, scheduled_datetime: datetime, duration_minutes: int = 120, modality: str = DefenseModality.IN_PERSON, location: str = "", notes: str = "", members: list[dict[str, Any]] | None = None, acknowledge_schedule_warning: bool = False, actor: User, request: HttpRequest | None = None) -> Defense:
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

    # Valida a comissão antes de gravar qualquer coisa. O rascunho em memória permite
    # usar as mesmas funções de conflito aplicadas quando um membro entra depois.
    draft = Defense(scheduled_datetime=scheduled_datetime, duration_minutes=duration_minutes, thesis=thesis)
    prepared_members: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    for item in members or []:
        role = item["role"]
        if role == CommitteeRole.PRESIDENT and any(prepared["role"] == CommitteeRole.PRESIDENT for prepared in prepared_members):
            raise ValidationError("Esta banca já possui um presidente.")
        if item.get("userId"):
            user = User.objects.filter(pk=item["userId"], is_active=True).first()
            if user is None:
                raise ValidationError("Usuário informado para a banca não existe.")
            _check_schedule_overlap(draft, user)
            warnings.extend(_institutional_warnings(draft, user.pk, user.name or user.email))
            prepared_members.append({"role": role, "kind": "institutional", "user": user, "name": user.name or user.email, "email": user.email, "institution": ""})
            continue
        email = str(item.get("email") or "").strip().lower()
        name = str(item.get("name") or "").strip() or email
        if not email:
            raise ValidationError("Informe o e-mail do membro externo.")
        if _has_external_schedule_overlap(draft, email):
            raise ValidationError("Este membro já participa de outra banca no mesmo período.")
        warnings.extend(_external_warnings(draft, email, name))
        prepared_members.append({"role": role, "kind": "external", "name": name, "email": email, "institution": str(item.get("institution") or "").strip()})
    if warnings and not acknowledge_schedule_warning:
        raise ScheduleWarningError(warnings)

    main_author = thesis.authors.filter(is_main_author=True).first()
    snapshot = {
        "thesisId": str(thesis.pk),
        "title": thesis.title,
        "referenceCode": thesis.reference_code,
        "courseName": thesis.course.name if thesis.course_id else "",
        "courseCode": thesis.course.code if thesis.course_id else "",
        "advisorName": thesis.advisor.name if thesis.advisor_id else "",
        "studentName": main_author.display_name if main_author else "",
    }
    defense = Defense.objects.create(
        thesis=thesis,
        thesis_snapshot=snapshot,
        scheduled_datetime=scheduled_datetime,
        duration_minutes=duration_minutes,
        modality=modality,
        location=clean_location,
        notes=notes.strip(),
        status=DefenseStatus.SCHEDULED,
        created_by_id=actor.pk,
    )
    record_defense_status_change(defense, "", actor, "Banca agendada.")

    for prepared in prepared_members:
        if prepared["kind"] == "institutional":
            member = DefenseMember.objects.create(
                defense=defense,
                role=prepared["role"],
                institutional_member_id=prepared["user"].pk,
                name=prepared["name"],
                email=prepared["email"],
            )
        else:
            invitation = create_external_invitation(name=prepared["name"], email=prepared["email"], institution=prepared["institution"], invited_by=actor, request=request)
            member = DefenseMember.objects.create(
                defense=defense,
                role=prepared["role"],
                external_invitation_id=invitation.pk,
                name=prepared["name"],
                email=prepared["email"],
                institution=prepared["institution"],
            )
        record_defense_member_invited(defense, member)

    previous_status = thesis.status
    thesis.status = ThesisStatus.DEFENSE_SCHEDULED
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_status,
        new_status=ThesisStatus.DEFENSE_SCHEDULED,
        changed_by_id=actor.pk if actor else None,
        reason="Defesa agendada.",
        metadata={"defenseId": str(defense.pk)},
    )
    record_defense_scheduled(defense)
    audit_defense(
        action=AuditAction.SCHEDULE,
        defense=defense,
        request=request,
        actor_user=actor,
        metadata=event_payload(AuditEvent.DEFENSE_SCHEDULED, scheduledDatetime=scheduled_datetime.isoformat()),
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
        changed_by_id=changed_by.pk if changed_by else None,
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
    audit_defense(
        action=AuditAction.UPDATE,
        defense=defense,
        request=request,
        actor_user=None,
        metadata=event_payload(AuditEvent.AUTO_STARTED_BY_SCHEDULE),
    )
    return defense


@transaction.atomic
def update_defense(defense: Defense, *, scheduled_datetime: datetime | None = None, duration_minutes: int | None = None, modality: str | None = None, location: str | None = None, notes: str | None = None, acknowledge_schedule_warning: bool = False, actor: User, request: HttpRequest | None = None) -> Defense:
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
        warnings = defense_schedule_warnings(defense)
        if warnings and not acknowledge_schedule_warning:
            raise ScheduleWarningError(warnings)
    defense.save(update_fields=["scheduled_datetime", "duration_minutes", "modality", "location", "notes", "updated_at"] + extra_fields)
    availability_reset = datetime_changed or duration_changed
    if availability_reset:
        reset_committee_availability(defense)

    audit_defense(
        action=AuditAction.UPDATE,
        defense=defense,
        request=request,
        actor_user=actor,
        metadata=event_payload(AuditEvent.DEFENSE_UPDATED, availabilityReset=availability_reset),
    )
    if datetime_changed or duration_changed:
        record_defense_rescheduled(defense)
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
        changed_by_id=actor.pk if actor else None,
        reason=cancel_reason,
        metadata={"defenseId": str(defense.pk)},
    )

    defense.previous_status = defense.status
    defense.status = DefenseStatus.CANCELLED
    defense.auto_start_suspended = False
    defense.save(update_fields=["status", "previous_status", "auto_start_suspended", "updated_at"])
    record_defense_status_change(defense, defense.previous_status, actor, cancel_reason)

    audit_defense(
        action=AuditAction.SCHEDULE,
        defense=defense,
        request=request,
        actor_user=actor,
        metadata=event_payload(AuditEvent.DEFENSE_CANCELLED, reason=reason),
    )
    record_defense_cancelled(defense, previous_status=defense.previous_status)
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

    audit_defense(
        action=AuditAction.SCHEDULE,
        defense=defense,
        request=request,
        actor_user=actor,
        metadata=event_payload(AuditEvent.DEFENSE_STARTED),
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
    if thesis.status in {ThesisStatus.PUBLISHED, ThesisStatus.ARCHIVED}:
        raise ValidationError("Não é possível encerrar a banca de uma monografia já publicada ou arquivada.")
    previous_thesis_status = thesis.status
    thesis.status = new_thesis_status
    thesis.save(update_fields=["status", "updated_at"])
    result_reason = f"Banca encerrada. Resultado: {DefenseResult(result).label}."
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=new_thesis_status,
        changed_by_id=closed_by.pk if closed_by else None,
        reason=result_reason,
        metadata={"defenseId": str(defense.pk), "defenseResult": result, "automatic": automatic},
    )

    defense.status = DefenseStatus.COMPLETED
    defense.save(update_fields=["status", "updated_at"])
    history_reason = AUTO_CLOSE_REASON if automatic else result_reason
    record_defense_status_change(defense, DefenseStatus.IN_PROGRESS, closed_by, history_reason)

    if new_thesis_status in APPROVED_DEFENSE_STATUSES:
        purge_thesis_versions(thesis, actor=closed_by, request=request)

    audit_defense(
        action=AuditAction.EVALUATE,
        defense=defense,
        request=request,
        actor_user=closed_by,
        metadata=event_payload(AuditEvent.DEFENSE_CLOSED, result=result, thesisNewStatus=new_thesis_status, trigger="AUTO_AFTER_LAST_EVALUATION" if automatic else "MANUAL"),
    )
    # Dentro do mesmo savepoint do encerramento: se o auto-close falhar, o evento é desfeito junto.
    record_defense_completed(defense, result, new_thesis_status, automatic)
    if new_thesis_status in APPROVED_DEFENSE_STATUSES:
        publish_if_ready(thesis, trigger=PublicationTrigger.DEFENSE_CLOSED, request=request)
    return defense


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

    if to_status in ACTIVE_DEFENSE_STATUSES and Defense.objects.filter(
        thesis_id=defense.thesis_id,
        status__in=ACTIVE_DEFENSE_STATUSES,
    ).exclude(pk=defense.pk).exists():
        raise ValidationError("Já existe outra banca ativa para esta monografia.")

    thesis = Thesis.objects.select_for_update().get(pk=defense.thesis_id)
    if thesis.status in {ThesisStatus.PUBLISHED, ThesisStatus.ARCHIVED}:
        raise ValidationError("Não é possível reverter a banca de uma monografia já publicada ou arquivada.")
    previous_thesis_status = thesis.status
    thesis.status = ThesisStatus.DEFENSE_SCHEDULED
    thesis.save(update_fields=["status", "updated_at"])
    ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_thesis_status,
        new_status=ThesisStatus.DEFENSE_SCHEDULED,
        changed_by_id=actor.pk if actor else None,
        reason=f"Status da banca revertido pelo administrador. {reason}".strip(),
        metadata={"defenseId": str(defense.pk)},
    )

    defense.status = to_status
    defense.previous_status = ""
    defense.auto_start_suspended = True
    defense.save(update_fields=["status", "previous_status", "auto_start_suspended", "updated_at"])
    record_defense_status_change(defense, from_status, actor, f"Status revertido pelo administrador. {reason.strip()}")

    audit_defense(
        action=AuditAction.UPDATE,
        defense=defense,
        request=request,
        actor_user=actor,
        metadata=event_payload(AuditEvent.STATUS_REVERTED_BY_ADMIN, fromStatus=from_status, toStatus=to_status, reason=reason),
    )
    return defense


def _defense_close_entries_queryset() -> QuerySet[ThesisStatusHistory]:
    """
    Return thesis history entries written by ``_finalize_defense``.

    Only the defense close stores both ``defenseId`` and ``defenseResult``; the
    manual thesis-level result (``record_defense_result``) has no ``defenseId``.

    Returns:
        QuerySet[ThesisStatusHistory]: Close entries, newest first.
    """
    return ThesisStatusHistory.objects.filter(metadata__has_keys=["defenseId", "defenseResult"]).order_by("-changed_at")


def defense_close_entries_prefetch() -> Prefetch:
    """
    Build the prefetch that lets list payloads read defense results without N+1 queries.

    Returns:
        Prefetch: Prefetch of ``thesis__status_history`` into ``thesis.defense_close_entries``.
    """
    return Prefetch("thesis__status_history", queryset=_defense_close_entries_queryset(), to_attr=DEFENSE_CLOSE_ENTRIES_ATTR)


def defense_result_code(defense: Defense) -> str | None:
    """
    Return the committee result computed when the defense was closed.

    Reads the result that ``_finalize_defense`` recorded (manual or automatic close)
    instead of recomputing it, so the payload always matches what was applied to
    the thesis.

    Args:
        defense: Defense session (``thesis.defense_close_entries`` used when prefetched).

    Returns:
        str | None: DefenseResult code, or None while the defense is not COMPLETED.
    """
    if defense.status != DefenseStatus.COMPLETED:
        return None
    close_entries = getattr(defense.thesis, DEFENSE_CLOSE_ENTRIES_ATTR, None)
    if close_entries is None:
        close_entries = list(_defense_close_entries_queryset().filter(thesis_id=defense.thesis_id, metadata__defenseId=str(defense.pk)))
    defense_id = str(defense.pk)
    for entry in close_entries:
        if entry.metadata.get("defenseId") == defense_id:
            return entry.metadata.get("defenseResult")
    return None


def defense_payload(defense: Defense, viewer: Any = None, viewer_context: DefenseViewerContext | None = None, avatar_urls: dict | None = None) -> dict[str, Any]:
    """
    Serialize a defense session with its committee members.

    Args:
        defense: Defense session.
        viewer: Authenticated user or ExternalInvitationPrincipal used to decide
            evaluation visibility per member (passed through to defense_member_payload).
        viewer_context: Viewer rights already resolved for this defense's thesis
            (``build_defense_viewer_context``); listings pass it to avoid queries per defense.
            Members' availability is shown only to managers and to the member themselves.

    Returns:
        dict[str, Any]: Defense payload including members list and ``result``/``resultLabel``
        (null until the defense is COMPLETED).
    """
    result_code = defense_result_code(defense)
    if viewer_context is None:
        viewer_context = build_defense_viewer_context(viewer, [defense.thesis_id])
    viewer_is_admin = viewer_context.is_admin
    viewer_can_manage = viewer_context.can_manage(defense.thesis_id)
    members = list(defense.members.all())
    if avatar_urls is None:
        avatar_urls = avatar_urls_for(m.institutional_member_id for m in members)
    return {
        "id": str(defense.pk),
        "thesis": str(defense.thesis_id),
        "thesisId": str(defense.thesis_id),
        "thesisSummary": thesis_summary_payload(defense.thesis, include_file_urls=not isinstance(viewer, ExternalInvitationPrincipal)),
        "scheduledDatetime": defense.scheduled_datetime.isoformat(),
        "durationMinutes": defense.duration_minutes,
        "modality": defense.modality,
        "modalityLabel": DefenseModality(defense.modality).label,
        "location": defense.location,
        "notes": defense.notes,
        "status": defense.status,
        "statusLabel": DefenseStatus(defense.status).label,
        "result": result_code,
        "resultLabel": DefenseResult(result_code).label if result_code else None,
        "createdAt": defense.created_at.isoformat(),
        "updatedAt": defense.updated_at.isoformat(),
        "members": [
            defense_member_payload(m, viewer=viewer, viewer_is_admin=viewer_is_admin, viewer_can_manage=viewer_can_manage, avatar_url=avatar_urls.get(m.institutional_member_id))
            for m in members
        ],
    }
