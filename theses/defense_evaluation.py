"""
Evaluation submission services for defense committee members.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest

from accounts.audit_events import event_payload
from accounts.audit_identifiers import AuditEvent
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction, User
from accounts.services import record_audit
from theses.audit import audit_defense
from theses.defense_scheduling import auto_close_after_last_evaluation
from theses.events import record_defense_evaluation_recorded
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseResult, DefenseStatus


@dataclass(frozen=True)
class EvaluationOutcome:
    """
    Result of recording a committee evaluation.

    Attributes:
        evaluation: Saved evaluation.
        auto_close_error: Message when the automatic close after the last evaluation
            failed (the evaluation is still saved), otherwise None.
    """

    evaluation: DefenseMemberEvaluation
    auto_close_error: str | None = None


def submit_committee_evaluation(defense: Defense, member: DefenseMember, *, decision: str, score=None, notes: str = "", actor: User | ExternalInvitationPrincipal, request: HttpRequest | None = None) -> DefenseMemberEvaluation:
    """
    Submit or update a committee member's evaluation and return only the evaluation.

    Thin wrapper over ``record_committee_evaluation`` kept for callers that do not
    need the automatic-close outcome; the automatic close still runs.

    Args:
        defense: Defense session.
        member: Committee member submitting the evaluation.
        decision: DefenseResult code (APPROVED, REJECTED, or APPROVED_WITH_REMARKS).
        score: Optional score between 0.00 and 10.00.
        notes: Optional evaluation notes.
        actor: Institutional user or ExternalInvitationPrincipal performing the action.
        request: HTTP request used for audit context.

    Returns:
        DefenseMemberEvaluation: Created or updated evaluation record.

    Raises:
        ValidationError: When the defense status, member role, identity, or score is invalid.
    """
    outcome = record_committee_evaluation(defense, member, decision=decision, score=score, notes=notes, actor=actor, request=request)
    return outcome.evaluation


def record_committee_evaluation(defense: Defense, member: DefenseMember, *, decision: str, score=None, notes: str = "", actor: User | ExternalInvitationPrincipal, request: HttpRequest | None = None) -> EvaluationOutcome:
    """
    Submit or update a committee member's evaluation for an in-progress defense.

    After the evaluation is saved, if every non-alternate member has evaluated, the
    defense is closed automatically through the same logic as ``close_defense``,
    in a separate savepoint: a failure there never undoes the evaluation and is
    reported in ``EvaluationOutcome.auto_close_error``.

    Alternates are blocked from evaluating. Only the member themselves (institutional
    or external) may submit their own evaluation. After the defense is COMPLETED,
    any attempt is recorded in the audit log and rejected.

    The blocked-after-close audit is committed outside the success transaction so it
    persists even though a ValidationError is raised.

    Args:
        defense: Defense session.
        member: Committee member submitting the evaluation.
        decision: DefenseResult code (APPROVED, REJECTED, or APPROVED_WITH_REMARKS).
        score: Optional score between 0.00 and 10.00.
        notes: Optional evaluation notes.
        actor: Institutional user or ExternalInvitationPrincipal performing the action.
        request: HTTP request used for audit context.

    Returns:
        EvaluationOutcome: Saved evaluation and the automatic-close error, if any.

    Raises:
        ValidationError: When the defense status, member role, identity, or score is invalid.
    """
    actor_user = actor if isinstance(actor, User) else None
    actor_ext = actor.invitation if isinstance(actor, ExternalInvitationPrincipal) else None

    # Tentativa após encerramento: audit persiste fora de transação, depois levanta exceção
    if defense.status == DefenseStatus.COMPLETED:
        audit_defense(
            action=AuditAction.EVALUATE,
            defense=defense,
            request=request,
            actor_user=actor_user,
            actor_external_invitation=actor_ext,
            metadata=event_payload(AuditEvent.EVALUATION_BLOCKED_AFTER_CLOSE, memberId=str(member.pk)),
        )
        raise ValidationError("A banca já foi encerrada. Avaliações não podem ser alteradas.")

    if defense.status != DefenseStatus.IN_PROGRESS:
        raise ValidationError("Avaliações só podem ser submetidas durante a sessão de defesa.")

    if member.role == CommitteeRole.ALTERNATE:
        raise PermissionDenied("Membros suplentes não podem submeter avaliações.")

    if member.defense_id != defense.pk:
        raise ValidationError("Membro não pertence a esta banca.")

    # Verificar identidade: cada membro avalia apenas como si mesmo
    if isinstance(actor, User):
        if member.institutional_member_id != actor.pk:
            raise ValidationError("Você só pode avaliar como membro desta banca.")
    elif isinstance(actor, ExternalInvitationPrincipal):
        if member.external_invitation_id != actor.invitation.pk:
            raise ValidationError("Você só pode avaliar como membro desta banca.")

    if decision not in DefenseResult.values:
        raise ValidationError("Decisão de avaliação inválida.")

    # Validar nota
    score_value = None
    if score is not None:
        try:
            score_value = Decimal(str(score))
        except InvalidOperation:
            raise ValidationError("Nota inválida.")
        if not (Decimal("0") <= score_value <= Decimal("10")):
            raise ValidationError("A nota deve ser entre 0,00 e 10,00.")

    blocked_after_close = False
    with transaction.atomic():
        # A verificação inicial usa o objeto carregado pela view, que pode estar
        # desatualizado: outra avaliação pode encerrar a banca nesse intervalo.
        # O status é revalidado sob lock antes de qualquer escrita.
        locked_defense = Defense.objects.select_for_update().get(pk=defense.pk)
        if locked_defense.status == DefenseStatus.COMPLETED:
            blocked_after_close = True
        elif locked_defense.status != DefenseStatus.IN_PROGRESS:
            raise ValidationError("Avaliações só podem ser submetidas durante a sessão de defesa.")
        else:
            defense = locked_defense
            evaluation, created = DefenseMemberEvaluation.objects.update_or_create(
                member=member,
                defaults={"decision": decision, "score": score_value, "notes": notes.strip()},
            )
            record_defense_evaluation_recorded(defense, member, is_update=not created)
            audit_defense(
                action=AuditAction.EVALUATE,
                defense=defense,
                request=request,
                actor_user=actor_user,
                actor_external_invitation=actor_ext,
                metadata=event_payload(
                    AuditEvent.EVALUATION_SUBMITTED,
                    memberId=str(member.pk),
                    decision=decision,
                    score=str(score_value) if score_value is not None else None,
                ),
            )
    if blocked_after_close:
        # Auditoria após o commit, como o fluxo original pretendia.
        audit_defense(
            action=AuditAction.EVALUATE,
            defense=defense,
            request=request,
            actor_user=actor_user,
            actor_external_invitation=actor_ext,
            metadata=event_payload(AuditEvent.EVALUATION_BLOCKED_AFTER_CLOSE, memberId=str(member.pk)),
        )
        raise ValidationError("A banca já foi encerrada. Avaliações não podem ser alteradas.")
    auto_close_error = auto_close_after_last_evaluation(defense, request=request)
    return EvaluationOutcome(evaluation=evaluation, auto_close_error=auto_close_error)


def defense_evaluation_payload(evaluation: DefenseMemberEvaluation, *, is_own: bool = False) -> dict[str, Any]:
    """
    Serialize a committee member's evaluation.

    When is_own is False only the decision and timestamp are included, to avoid
    exposing another member's score and notes.

    Args:
        evaluation: DefenseMemberEvaluation instance.
        is_own: True when the viewer is the member who submitted the evaluation or an admin.

    Returns:
        dict[str, Any]: Serialized evaluation payload.
    """
    base: dict[str, Any] = {
        "id": str(evaluation.pk),
        "decision": evaluation.decision,
        "decisionLabel": DefenseResult(evaluation.decision).label,
        "submittedAt": evaluation.submitted_at.isoformat(),
    }
    if not is_own:
        return base
    base.update({
        "score": str(evaluation.score) if evaluation.score is not None else None,
        "notes": evaluation.notes,
        "updatedAt": evaluation.updated_at.isoformat(),
    })
    return base
