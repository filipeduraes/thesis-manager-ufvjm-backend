"""
Committee member management services for thesis defense sessions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction, User
from accounts.services import create_external_invitation, record_audit, revoke_external_invitation, user_avatar_url
from theses.models import ACTIVE_DEFENSE_STATUSES, AvailabilityStatus, CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis
from theses.services import theses_visible_to, user_is_admin, user_is_advisor, user_is_coordinator


def coordinator_managed_theses(coordinator: User) -> QuerySet[Thesis]:
    """
    Return the theses whose defenses a coordinator may see and manage.

    Reuses ``theses_visible_to`` (course coordinator, coordinator e-mail and active
    course assignments) instead of duplicating the course link rules. Theses the
    user authors are excluded so that a coordinator never manages their own defense.

    Args:
        coordinator: Institutional user with the coordinator role.

    Returns:
        QuerySet[Thesis]: Theses of the courses the user coordinates.
    """
    return theses_visible_to(coordinator).exclude(authors__author_id=coordinator.pk)


def _can_manage_defense(actor: User, thesis_id) -> bool:
    """
    Return whether the actor may schedule or manage defenses for a thesis.

    Administrators always have permission. Coordinators may manage defenses of
    theses from the courses they coordinate (``coordinator_managed_theses``). An
    advisor may manage defenses only for the thesis where they are the assigned advisor.

    Args:
        actor: Authenticated institutional user.
        thesis_id: Primary key of the target thesis.

    Returns:
        bool: True when the actor is authorized.
    """
    if not isinstance(actor, User):
        # Sessões de convite externo nunca gerenciam bancas.
        return False
    if user_is_admin(actor):
        return True
    if user_is_coordinator(actor) and coordinator_managed_theses(actor).filter(pk=thesis_id).exists():
        return True
    if not user_is_advisor(actor):
        return False
    # Orientador só pode gerenciar a própria tese
    advisor_id = Thesis.objects.values_list("advisor_id", flat=True).get(pk=thesis_id)
    return advisor_id == actor.pk


def resolve_committee_member(defense: Defense, principal: User | ExternalInvitationPrincipal) -> DefenseMember | None:
    """
    Return the DefenseMember for the current request principal within a defense.

    Works for both institutional User and ExternalInvitationPrincipal.

    Args:
        defense: Defense session.
        principal: Authenticated user or ExternalInvitationPrincipal.

    Returns:
        DefenseMember | None: Matching member record, or None if not found.
    """
    if isinstance(principal, ExternalInvitationPrincipal):
        return DefenseMember.objects.filter(defense=defense, external_invitation=principal.invitation).first()
    if isinstance(principal, User) and principal.is_authenticated:
        return DefenseMember.objects.filter(defense=defense, institutional_member=principal).first()
    return None


def defense_member_payload(member: DefenseMember, viewer: User | ExternalInvitationPrincipal | None = None, viewer_is_admin: bool | None = None) -> dict[str, Any]:
    """
    Serialize a committee member.

    Evaluation visibility: the member themselves or an admin may see it.
    Coordinators do not see other members' evaluations (pending decision — registered
    in the final report as: coordinator evaluation visibility not yet decided).

    ``avatarUrl`` comes from the institutional member's Google account (null for
    external members and users without a picture); prefetch it with
    ``google_social_accounts_prefetch`` to avoid one query per member.

    Args:
        member: Committee member instance.
        viewer: Authenticated user, ExternalInvitationPrincipal, or None.
        viewer_is_admin: Precomputed admin flag for ``viewer``; when None it is resolved
            here (callers serializing many members pass it to avoid a query per member).

    Returns:
        dict[str, Any]: Member payload with optional evaluation field.
    """
    payload: dict[str, Any] = {
        "id": str(member.pk),
        "role": member.role,
        "roleLabel": CommitteeRole(member.role).label,
        "name": member.name,
        "email": member.email,
        "institution": member.institution,
        "hasConflict": member.has_conflict,
        "conflictReason": member.conflict_reason if member.has_conflict else "",
        "isInstitutional": member.institutional_member_id is not None,
        "avatarUrl": user_avatar_url(member.institutional_member) if member.institutional_member_id else None,
        "availabilityStatus": member.availability_status,
        "availabilityStatusLabel": AvailabilityStatus(member.availability_status).label,
        "availabilityRespondedAt": member.availability_responded_at.isoformat() if member.availability_responded_at else None,
        "availabilityNote": member.availability_note,
        "createdAt": member.created_at.isoformat(),
        "evaluation": None,
    }

    can_see_eval = False
    if viewer is not None:
        if isinstance(viewer, ExternalInvitationPrincipal):
            can_see_eval = (member.external_invitation_id is not None and str(member.external_invitation_id) == str(viewer.invitation.pk))
        elif isinstance(viewer, User) and (viewer_is_admin if viewer_is_admin is not None else user_is_admin(viewer)):
            can_see_eval = True
        elif isinstance(viewer, User) and member.institutional_member_id == viewer.pk:
            can_see_eval = True

    if can_see_eval:
        try:
            ev = member.evaluation
            payload["evaluation"] = {
                "id": str(ev.pk),
                "decision": ev.decision,
                "score": str(ev.score) if ev.score is not None else None,
                "notes": ev.notes,
                "submittedAt": ev.submitted_at.isoformat(),
                "updatedAt": ev.updated_at.isoformat(),
            }
        except ObjectDoesNotExist:
            pass

    return payload


def _has_schedule_overlap(defense: Defense, user_id: Any) -> bool:
    """
    Return whether the user belongs to another active defense overlapping this one.

    Two defenses overlap when their half-open intervals [start, start+duration)
    intersect: A.start < B.end AND B.start < A.end. Adjacent sessions (one ends
    exactly when the other starts) are not considered overlapping. The interval
    is read from the in-memory ``defense``, so unsaved schedule changes are honored.

    Args:
        defense: Defense whose (possibly unsaved) interval is checked.
        user_id: Primary key of the institutional user to check.

    Returns:
        bool: True when an overlapping active defense is found.
    """
    this_start = defense.scheduled_datetime
    this_end = this_start + timedelta(minutes=defense.duration_minutes)
    base_qs = Defense.objects.filter(members__institutional_member_id=user_id, status__in=ACTIVE_DEFENSE_STATUSES)
    candidates = base_qs.exclude(pk=defense.pk).values("scheduled_datetime", "duration_minutes")
    for other in candidates:
        other_start = other["scheduled_datetime"]
        other_end = other_start + timedelta(minutes=other["duration_minutes"])
        if this_start < other_end and other_start < this_end:
            return True
    return False


def _check_schedule_overlap(defense: Defense, user: User) -> None:
    """
    Raise ValidationError when user already belongs to an overlapping active defense.

    Args:
        defense: Defense the user is being added to.
        user: Institutional user to check.

    Raises:
        ValidationError: When an overlapping active defense is found.
    """
    if _has_schedule_overlap(defense, user.pk):
        raise ValidationError("Este membro já participa de outra banca no mesmo período.")


def check_committee_schedule_conflicts(defense: Defense) -> None:
    """
    Validate that no institutional member of the defense has an overlapping active defense.

    Used when the defense date, time or duration changes. External members are not
    checked because they have no cross-defense identity (one invitation per membership).

    Args:
        defense: Defense carrying the new (possibly unsaved) schedule.

    Raises:
        ValidationError: Naming the first institutional member found in conflict.
    """
    institutional_members = defense.members.filter(institutional_member__isnull=False).order_by("created_at")
    for member in institutional_members:
        if _has_schedule_overlap(defense, member.institutional_member_id):
            raise ValidationError(f"O membro {member.name} já participa de outra banca no mesmo período.")


@transaction.atomic
def add_institutional_member(defense: Defense, role: str, user: User, *, conflict_reason: str = "", actor: User, request: HttpRequest | None = None) -> DefenseMember:
    """
    Add an institutional user as a committee member.

    Args:
        defense: Defense session.
        role: CommitteeRole code.
        user: Institutional user to add.
        conflict_reason: If non-empty, marks the member as having a conflict of interest.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        DefenseMember: Created member record.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When the defense status, role, or schedule overlap is invalid.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    if defense.status not in ACTIVE_DEFENSE_STATUSES:
        raise ValidationError("Só é possível adicionar membros a bancas agendadas ou em andamento.")
    if role not in CommitteeRole.values:
        raise ValidationError("Papel de membro inválido.")
    if role == CommitteeRole.PRESIDENT and defense.members.filter(role=CommitteeRole.PRESIDENT).exists():
        raise ValidationError("Esta banca já possui um presidente.")
    _check_schedule_overlap(defense, user)

    has_conflict = bool(conflict_reason.strip())
    member = DefenseMember.objects.create(
        defense=defense,
        role=role,
        institutional_member=user,
        name=user.name or user.email,
        email=user.email,
        institution="",
        has_conflict=has_conflict,
        conflict_reason=conflict_reason.strip(),
    )
    record_audit(
        action=AuditAction.CREATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "MEMBER_ADDED", "memberId": str(member.pk), "role": role, "type": "institutional"},
    )
    return member


@transaction.atomic
def add_external_member(defense: Defense, role: str, *, name: str, email: str, institution: str = "", conflict_reason: str = "", actor: User, request: HttpRequest | None = None) -> tuple[DefenseMember, str]:
    """
    Add an external member via the ExternalInvitation flow.

    The readable access token is returned once so the caller can hand it to the
    member; only its SHA-256 hash is stored. It must never be logged or audited.

    Args:
        defense: Defense session.
        role: CommitteeRole code.
        name: External member's full name.
        email: External member's email address.
        institution: External member's institution name.
        conflict_reason: If non-empty, marks the member as having a conflict of interest.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Returns:
        tuple[DefenseMember, str]: Created member record (with linked invitation) and
        the one-time readable access token.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When the defense status, role, or input data is invalid.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    if defense.status not in ACTIVE_DEFENSE_STATUSES:
        raise ValidationError("Só é possível adicionar membros a bancas agendadas ou em andamento.")
    if role not in CommitteeRole.values:
        raise ValidationError("Papel de membro inválido.")
    if role == CommitteeRole.PRESIDENT and defense.members.filter(role=CommitteeRole.PRESIDENT).exists():
        raise ValidationError("Esta banca já possui um presidente.")

    clean_name = " ".join((name or "").split())
    if not clean_name:
        raise ValidationError("Informe o nome do membro externo.")
    clean_email = (email or "").strip().lower()
    if not clean_email:
        raise ValidationError("Informe o e-mail do membro externo.")

    invitation, access_token = create_external_invitation(
        name=clean_name,
        email=clean_email,
        institution=institution.strip(),
        invited_by=actor,
        request=request,
    )
    has_conflict = bool(conflict_reason.strip())
    member = DefenseMember.objects.create(
        defense=defense,
        role=role,
        external_invitation=invitation,
        name=clean_name,
        email=clean_email,
        institution=institution.strip(),
        has_conflict=has_conflict,
        conflict_reason=conflict_reason.strip(),
    )
    record_audit(
        action=AuditAction.CREATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "MEMBER_ADDED", "memberId": str(member.pk), "role": role, "type": "external", "invitationId": str(invitation.pk)},
    )
    return member, access_token


@transaction.atomic
def remove_member(defense: Defense, member: DefenseMember, *, actor: User, request: HttpRequest | None = None) -> None:
    """
    Remove a committee member from a scheduled (not yet started) defense.

    Blocked once IN_PROGRESS or later: committee composition is frozen when the
    session starts so the evaluation quorum for close_defense stays stable.
    Also blocked if the member already submitted an evaluation, since deleting
    the member would cascade-delete the evaluation record.

    Args:
        defense: Defense session.
        member: Member to remove.
        actor: Admin, coordinator, or thesis advisor performing the action.
        request: HTTP request used for audit context.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When removal is not allowed.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    if defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Membros só podem ser removidos enquanto a banca está agendada (antes de iniciar).")
    if member.defense_id != defense.pk:
        raise ValidationError("Membro não pertence a esta banca.")
    try:
        _ = member.evaluation  # noqa: F841
        raise ValidationError("Não é possível remover um membro que já submeteu avaliação.")
    except ObjectDoesNotExist:
        pass

    member_id = str(member.pk)
    role = member.role
    invitation = member.external_invitation
    member.delete()

    if invitation is not None:
        revoke_external_invitation(invitation, revoked_by=actor, request=request)

    record_audit(
        action=AuditAction.DELETE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "MEMBER_REMOVED", "memberId": member_id, "role": role},
    )


def pending_availability_members(defense: Defense) -> list[DefenseMember]:
    """
    Return the non-alternate members who have not confirmed availability yet.

    Alternates do not block the defense: only evaluating members (president and
    members) must confirm before the session can start.

    Args:
        defense: Defense session.

    Returns:
        list[DefenseMember]: Members with PENDING or DECLINED availability, in the order they were added.
    """
    evaluating_members = defense.members.exclude(role=CommitteeRole.ALTERNATE)
    return list(evaluating_members.exclude(availability_status=AvailabilityStatus.CONFIRMED).order_by("created_at"))


def defense_availability_summary(defense: Defense) -> dict[str, Any]:
    """
    Summarize committee availability for the defense detail payload.

    ``allMembersConfirmed`` is True only when the defense has at least one
    non-alternate member and all of them confirmed.

    Args:
        defense: Defense session.

    Returns:
        dict[str, Any]: ``allMembersConfirmed`` and ``pendingConfirmations``.
    """
    pending_members = pending_availability_members(defense)
    has_evaluating_members = defense.members.exclude(role=CommitteeRole.ALTERNATE).exists()
    pending_confirmations = [
        {"memberId": str(member.pk), "name": member.name, "role": member.role, "roleLabel": CommitteeRole(member.role).label, "availabilityStatus": member.availability_status}
        for member in pending_members
    ]
    return {"allMembersConfirmed": has_evaluating_members and not pending_members, "pendingConfirmations": pending_confirmations}


def _member_belongs_to_principal(member: DefenseMember, principal: User | ExternalInvitationPrincipal) -> bool:
    """
    Return whether the committee member record represents the request principal.

    Args:
        member: Committee member.
        principal: Institutional user or external invitation principal.

    Returns:
        bool: True when the principal is that member.
    """
    if isinstance(principal, ExternalInvitationPrincipal):
        return member.external_invitation_id is not None and member.external_invitation_id == principal.invitation.pk
    return member.institutional_member_id is not None and member.institutional_member_id == principal.pk


@transaction.atomic
def respond_availability(defense: Defense, member: DefenseMember, *, availability_status: str, note: str = "", actor: User | ExternalInvitationPrincipal, request: HttpRequest | None = None) -> DefenseMember:
    """
    Record a committee member's availability answer for a scheduled defense.

    Only the member themselves may answer (institutional user or the external
    session of their own invitation). The note is stored on the member but not
    copied into the audit log.

    Args:
        defense: Defense session (must be SCHEDULED).
        member: Committee member answering.
        availability_status: CONFIRMED or DECLINED.
        note: Optional free-text note.
        actor: Institutional user or ExternalInvitationPrincipal.
        request: HTTP request used for audit context.

    Returns:
        DefenseMember: Updated member.

    Raises:
        PermissionDenied: When the actor is not this member.
        ValidationError: When the defense is not SCHEDULED or the status is invalid.
    """
    if member.defense_id != defense.pk or not _member_belongs_to_principal(member, actor):
        raise PermissionDenied("Você só pode responder a disponibilidade como você mesmo.")
    if availability_status not in (AvailabilityStatus.CONFIRMED, AvailabilityStatus.DECLINED):
        raise ValidationError({"status": "Informe CONFIRMED ou DECLINED."})
    locked_defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if locked_defense.status != DefenseStatus.SCHEDULED:
        raise ValidationError("Só é possível responder a disponibilidade de uma banca agendada.")

    member = DefenseMember.objects.select_for_update().get(pk=member.pk)
    previous_status = member.availability_status
    member.availability_status = availability_status
    member.availability_responded_at = timezone.now()
    member.availability_note = note.strip()
    member.save(update_fields=["availability_status", "availability_responded_at", "availability_note"])

    actor_user = actor if isinstance(actor, User) else None
    actor_external = actor.invitation if isinstance(actor, ExternalInvitationPrincipal) else None
    record_audit(
        action=AuditAction.UPDATE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor_user,
        actor_external_invitation=actor_external,
        metadata={"event": "AVAILABILITY_RESPONDED", "memberId": str(member.pk), "from": previous_status, "to": availability_status},
    )
    return member


def reset_committee_availability(defense: Defense) -> int:
    """
    Reset every member's availability answer to PENDING.

    Called when the defense date, time or duration actually changes, because
    previous answers referred to another slot.

    Args:
        defense: Defense session.

    Returns:
        int: Number of members reset.
    """
    return defense.members.update(availability_status=AvailabilityStatus.PENDING, availability_responded_at=None, availability_note="")


@transaction.atomic
def regenerate_external_member_token(defense: Defense, member: DefenseMember, *, actor: User, request: HttpRequest | None = None) -> tuple[DefenseMember, str]:
    """
    Replace an external member's invitation and return the new readable token once.

    The current invitation is revoked (its sessions stop working) and a new one is
    created and linked to the same member, keeping availability and evaluation.

    Args:
        defense: Defense session (must be SCHEDULED or IN_PROGRESS).
        member: External committee member.
        actor: Admin, coordinator, or thesis advisor managing the defense.
        request: HTTP request used for audit context.

    Returns:
        tuple[DefenseMember, str]: Updated member and the new one-time access token.

    Raises:
        PermissionDenied: When the actor is not authorized to manage this defense.
        ValidationError: When the member is not external or the defense is not active.
    """
    if not _can_manage_defense(actor, defense.thesis_id):
        raise PermissionDenied()
    if member.defense_id != defense.pk:
        raise ValidationError("Membro não pertence a esta banca.")
    locked_defense = Defense.objects.select_for_update().get(pk=defense.pk)
    if locked_defense.status not in ACTIVE_DEFENSE_STATUSES:
        raise ValidationError("Só é possível gerar novo token para bancas agendadas ou em andamento.")
    # FOR UPDATE não pode ser combinado com o LEFT JOIN do convite (nulo para institucionais).
    member = DefenseMember.objects.select_for_update().get(pk=member.pk)
    previous_invitation = member.external_invitation
    if previous_invitation is None:
        raise ValidationError("Somente membros externos possuem token de acesso.")

    revoke_external_invitation(previous_invitation, revoked_by=actor, request=request)
    new_invitation, access_token = create_external_invitation(name=member.name, email=member.email, institution=member.institution, invited_by=actor, request=request)
    member.external_invitation = new_invitation
    member.save(update_fields=["external_invitation"])
    record_audit(
        action=AuditAction.INVITE,
        entity_type="Defense",
        entity_id=str(defense.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata={"event": "EXTERNAL_TOKEN_REGENERATED", "memberId": str(member.pk), "previousInvitationId": str(previous_invitation.pk), "invitationId": str(new_invitation.pk)},
    )
    return member, access_token
