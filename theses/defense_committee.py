"""
Committee member management services for thesis defense sessions.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction, User
from accounts.services import create_external_invitation, record_audit, revoke_external_invitation
from theses.models import ACTIVE_DEFENSE_STATUSES, CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis
from theses.services import user_is_admin, user_is_advisor, user_is_coordinator


def _can_manage_defense(actor: User, thesis_id) -> bool:
    """
    Return whether the actor may schedule or manage defenses for a thesis.

    Administrators and coordinators always have permission. An advisor may manage
    defenses only for the thesis where they are the assigned advisor.

    Args:
        actor: Authenticated institutional user.
        thesis_id: Primary key of the target thesis.

    Returns:
        bool: True when the actor is authorized.
    """
    if user_is_admin(actor) or user_is_coordinator(actor):
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


def defense_member_payload(member: DefenseMember, viewer: User | ExternalInvitationPrincipal | None = None) -> dict[str, Any]:
    """
    Serialize a committee member.

    Evaluation visibility: the member themselves or an admin may see it.
    Coordinators do not see other members' evaluations (pending decision — registered
    in the final report as: coordinator evaluation visibility not yet decided).

    Args:
        member: Committee member instance.
        viewer: Authenticated user, ExternalInvitationPrincipal, or None.

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
        "createdAt": member.created_at.isoformat(),
        "evaluation": None,
    }

    can_see_eval = False
    if viewer is not None:
        if isinstance(viewer, ExternalInvitationPrincipal):
            can_see_eval = (member.external_invitation_id is not None and str(member.external_invitation_id) == str(viewer.invitation.pk))
        elif isinstance(viewer, User) and user_is_admin(viewer):
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


def _check_schedule_overlap(defense: Defense, user: User) -> None:
    """
    Raise ValidationError when user already belongs to an overlapping active defense.

    Two defenses overlap when their half-open intervals [start, start+duration)
    intersect: A.start < B.end AND B.start < A.end. Adjacent sessions (one ends
    exactly when the other starts) are not considered overlapping.

    Args:
        defense: Defense the user is being added to.
        user: Institutional user to check.

    Raises:
        ValidationError: When an overlapping active defense is found.
    """
    this_start = defense.scheduled_datetime
    this_end = this_start + timedelta(minutes=defense.duration_minutes)
    base_qs = Defense.objects.filter(members__institutional_member=user, status__in=ACTIVE_DEFENSE_STATUSES)
    candidates = base_qs.exclude(pk=defense.pk).values("scheduled_datetime", "duration_minutes")
    for other in candidates:
        other_start = other["scheduled_datetime"]
        other_end = other_start + timedelta(minutes=other["duration_minutes"])
        if this_start < other_end and other_start < this_end:
            raise ValidationError("Este membro já participa de outra banca no mesmo período.")


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
def add_external_member(defense: Defense, role: str, *, name: str, email: str, institution: str = "", conflict_reason: str = "", actor: User, request: HttpRequest | None = None) -> DefenseMember:
    """
    Add an external member via the ExternalInvitation flow.

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
        DefenseMember: Created member record with linked invitation.

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

    invitation, _token = create_external_invitation(
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
    return member


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
