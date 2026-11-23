"""
Idempotent handler that turns domain events into e-mail notifications.

The handler does not depend on the broker client: the RabbitMQ adapter decodes
the message, calls ``handle_event`` and acks or rejects based on the outcome.
"""

from __future__ import annotations

import enum
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from events.catalog import PAYLOAD_KEYS, EventType, is_known_event
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from events.outbox import EventPayload
from notifications.delivery import send_notification
from accounts.concurrency import database_for_model
from notifications.invitations import plan_external_member_invited
from notifications.models import Notification, NotificationTemplate, NotificationTemplateCode
from notifications.plans import EntityNotFoundError, NotificationPlan
from notifications.recipients import Recipient, defense_member, evaluating_committee_members, thesis_advisor, thesis_authors, unique_recipients
from notifications.rendering import RenderedMessage, TemplateContext, render_notification
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseModality, DefenseResult, Thesis

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "notifications"
DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")


class HandleOutcome(enum.Enum):
    """
    Result of handling one event, mapped to ack/reject by the broker adapter.
    """

    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"


def _get_thesis(thesis_id: str) -> Thesis:
    """
    Load the thesis referenced by an event.

    Args:
        thesis_id: Thesis identifier from the payload.

    Returns:
        Thesis: Thesis with advisor loaded.

    Raises:
        EntityNotFoundError: When the thesis does not exist.
    """
    thesis = Thesis.objects.filter(pk=thesis_id).first()
    if thesis is None:
        raise EntityNotFoundError("Thesis referenced by the event was not found")
    return thesis


def _get_defense(defense_id: str) -> Defense:
    """
    Load the defense referenced by an event.

    Args:
        defense_id: Defense identifier from the payload.

    Returns:
        Defense: Defense instance.

    Raises:
        EntityNotFoundError: When the defense does not exist.
    """
    defense = Defense.objects.filter(pk=defense_id).first()
    if defense is None:
        raise EntityNotFoundError("Defense referenced by the event was not found")
    return defense


def _format_datetime(raw_value: str) -> str:
    """
    Format an ISO datetime from a payload for Brazilian readers.

    Args:
        raw_value: ISO 8601 datetime.

    Returns:
        str: Date and time in America/Sao_Paulo, e.g. ``05/12/2026 às 14:00``.
    """
    parsed = parse_datetime(raw_value)
    local_value = parsed.astimezone(DISPLAY_TIMEZONE)
    return local_value.strftime("%d/%m/%Y às %H:%M")


def _plan_thesis_submitted(payload: EventPayload) -> NotificationPlan:
    """
    Notify the advisor that a version was submitted.

    Args:
        payload: ``thesis.submitted`` payload.

    Returns:
        NotificationPlan: Plan for the advisor.
    """
    thesis = _get_thesis(payload["thesisId"])
    authors = thesis_authors(thesis)
    author_names = ", ".join(author.name for author in authors) or "O(a) aluno(a)"
    context: TemplateContext = {"author_names": author_names, "version_number": payload["versionNumber"]}
    return NotificationPlan(NotificationTemplateCode.THESIS_SUBMITTED, thesis, thesis_advisor(thesis), context)


def _plan_defense_scheduled(payload: EventPayload) -> NotificationPlan:
    """
    Notify authors and advisor about the scheduled defense.

    Args:
        payload: ``defense.scheduled`` payload.

    Returns:
        NotificationPlan: Plan for authors and advisor.
    """
    thesis = _get_thesis(payload["thesisId"])
    recipients = unique_recipients(thesis_authors(thesis), thesis_advisor(thesis))
    modality_label = DefenseModality(payload["modality"]).label.lower()
    context: TemplateContext = {"scheduled_at": _format_datetime(payload["scheduledDatetime"]), "duration_minutes": payload["durationMinutes"], "modality_label": modality_label}
    return NotificationPlan(NotificationTemplateCode.DEFENSE_SCHEDULED, thesis, recipients, context)


def _plan_defense_member_invited(payload: EventPayload) -> NotificationPlan:
    """
    Plan the invitation e-mail of one committee member.

    Args:
        payload: ``defense.member_invited`` payload.

    Returns:
        NotificationPlan: Plan for the invited member.

    Raises:
        EntityNotFoundError: When the member no longer exists.
    """
    member = DefenseMember.objects.filter(pk=payload["memberId"]).first()
    if member is None:
        raise EntityNotFoundError("Defense member referenced by the event was not found")
    defense = _get_defense(payload["defenseId"])
    thesis = _get_thesis(payload["thesisId"])
    context: TemplateContext = {"scheduled_at": _format_datetime(defense.scheduled_datetime.isoformat())}
    return NotificationPlan(NotificationTemplateCode.DEFENSE_MEMBER_INVITED, thesis, defense_member(member), context)


def _plan_defense_cancelled(payload: EventPayload) -> NotificationPlan:
    """Notify authors and advisor about a cancelled defense."""
    thesis = _get_thesis(payload["thesisId"])
    return NotificationPlan(NotificationTemplateCode.DEFENSE_CANCELLED, thesis, unique_recipients(thesis_authors(thesis), thesis_advisor(thesis)))


def _plan_defense_rescheduled(payload: EventPayload) -> NotificationPlan:
    """Notify authors, advisor and evaluating members about a new schedule."""
    defense = _get_defense(payload["defenseId"])
    thesis = _get_thesis(payload["thesisId"])
    recipients = unique_recipients(thesis_authors(thesis), thesis_advisor(thesis), evaluating_committee_members(defense))
    context: TemplateContext = {"scheduled_at": _format_datetime(payload["scheduledDatetime"])}
    return NotificationPlan(NotificationTemplateCode.DEFENSE_RESCHEDULED, thesis, recipients, context)


def _plan_thesis_review_recorded(payload: EventPayload) -> NotificationPlan:
    """Notify the authors that the advisor recorded a review."""
    from theses.models import ReviewDecision

    thesis = _get_thesis(payload["thesisId"])
    decision = payload["reviewDecision"]
    context: TemplateContext = {"decision": ReviewDecision(decision).label if decision in ReviewDecision.values else decision}
    return NotificationPlan(NotificationTemplateCode.THESIS_REVIEW_RECORDED, thesis, thesis_authors(thesis), context)


def _plan_evaluation_recorded(payload: EventPayload) -> NotificationPlan:
    """
    Notify the advisor of the evaluation progress, without revealing any decision.

    Args:
        payload: ``defense.evaluation_recorded`` payload.

    Returns:
        NotificationPlan: Plan for the advisor.
    """
    defense = _get_defense(payload["defenseId"])
    thesis = _get_thesis(payload["thesisId"])
    evaluating_members = defense.members.exclude(role=CommitteeRole.ALTERNATE)
    required = evaluating_members.count()
    submitted = DefenseMemberEvaluation.objects.filter(member__in=evaluating_members).count()
    context: TemplateContext = {"evaluations_submitted": submitted, "evaluations_required": required}
    return NotificationPlan(NotificationTemplateCode.DEFENSE_EVALUATION_RECORDED, thesis, thesis_advisor(thesis), context)


def _plan_defense_completed(payload: EventPayload) -> NotificationPlan:
    """
    Notify authors, advisor and evaluating members of the committee result.

    Args:
        payload: ``defense.completed`` payload.

    Returns:
        NotificationPlan: Plan for authors, advisor and committee.
    """
    defense = _get_defense(payload["defenseId"])
    thesis = _get_thesis(payload["thesisId"])
    recipients = unique_recipients(thesis_authors(thesis), thesis_advisor(thesis), evaluating_committee_members(defense))
    context: TemplateContext = {"result_label": DefenseResult(payload["result"]).label}
    return NotificationPlan(NotificationTemplateCode.DEFENSE_COMPLETED, thesis, recipients, context)


def _plan_defense_result_recorded(payload: EventPayload) -> NotificationPlan:
    """
    Notify authors and advisor of a result recorded by the coordination.

    Args:
        payload: ``thesis.defense_result_recorded`` payload.

    Returns:
        NotificationPlan: Plan for authors and advisor.
    """
    thesis = _get_thesis(payload["thesisId"])
    recipients = unique_recipients(thesis_authors(thesis), thesis_advisor(thesis))
    context: TemplateContext = {"result_label": DefenseResult(payload["result"]).label}
    return NotificationPlan(NotificationTemplateCode.THESIS_DEFENSE_RESULT_RECORDED, thesis, recipients, context)


def _plan_thesis_published(payload: EventPayload) -> NotificationPlan:
    """
    Notify authors and advisor of the publication.

    Args:
        payload: ``thesis.published`` payload.

    Returns:
        NotificationPlan: Plan for authors and advisor.
    """
    thesis = _get_thesis(payload["thesisId"])
    recipients = unique_recipients(thesis_authors(thesis), thesis_advisor(thesis))
    return NotificationPlan(NotificationTemplateCode.THESIS_PUBLISHED, thesis, recipients)


PLANNERS: dict[str, Callable[[EventPayload], NotificationPlan]] = {
    EventType.THESIS_SUBMITTED: _plan_thesis_submitted,
    EventType.DEFENSE_SCHEDULED: _plan_defense_scheduled,
    EventType.DEFENSE_MEMBER_INVITED: _plan_defense_member_invited,
    EventType.DEFENSE_CANCELLED: _plan_defense_cancelled,
    EventType.DEFENSE_RESCHEDULED: _plan_defense_rescheduled,
    EventType.THESIS_REVIEW_RECORDED: _plan_thesis_review_recorded,
    EventType.DEFENSE_EVALUATION_RECORDED: _plan_evaluation_recorded,
    EventType.DEFENSE_COMPLETED: _plan_defense_completed,
    EventType.THESIS_DEFENSE_RESULT_RECORDED: _plan_defense_result_recorded,
    EventType.THESIS_PUBLISHED: _plan_thesis_published,
}

# Planejadores que precisam do id do evento (ex.: emissão do token, que só vale
# para o evento mais recente do convite).
EVENT_AWARE_PLANNERS: dict[str, Callable[[UUID, EventPayload], NotificationPlan]] = {
    EventType.DEFENSE_EXTERNAL_MEMBER_INVITED: plan_external_member_invited,
}


def _plan_event(envelope: EventEnvelope) -> NotificationPlan:
    """
    Run the planner registered for the event type.

    Args:
        envelope: Event being handled.

    Returns:
        NotificationPlan: What to send.

    Raises:
        EntityNotFoundError: When a referenced record no longer exists.
    """
    event_aware_planner = EVENT_AWARE_PLANNERS.get(envelope.event_type)
    if event_aware_planner is not None:
        return event_aware_planner(envelope.event_id, envelope.payload)
    planner = PLANNERS[envelope.event_type]
    return planner(envelope.payload)


@dataclass
class PlannedDelivery:
    """
    Stored notification plus the content to send when it differs from the stored copy.

    Attributes:
        notification: Persisted notification (never holds secrets).
        content: Message with secret values, kept only in memory; None to send the stored copy.
        after_send: Callback run when the backend accepts the message.
    """

    notification: Notification
    content: RenderedMessage | None
    after_send: Callable[[Notification], None] | None


def _create_notifications(envelope: EventEnvelope, plan: NotificationPlan) -> list[PlannedDelivery]:
    """
    Render and store one pending notification per recipient.

    Values in ``plan.secret_context`` are rendered only into the in-memory message
    that will be sent; the stored notification keeps the values of ``plan.context``.

    Args:
        envelope: Event being handled.
        plan: Template, recipients and context for the event.

    Returns:
        list[PlannedDelivery]: Created notifications (empty when the template is inactive).
    """
    template = NotificationTemplate.objects.filter(code=plan.template_code, is_active=True).first()
    if template is None:
        logger.warning("Event %s (%s): template %s missing or inactive; no notification created", envelope.event_id, envelope.event_type, plan.template_code)
        return []
    outbox_event = OutboxEvent.objects.filter(pk=envelope.event_id).first()
    base_context: TemplateContext = {"thesis_title": plan.thesis.title, "reference_code": plan.thesis.reference_code, "action_url": settings.FRONTEND_URL, **plan.context}
    deliveries: list[PlannedDelivery] = []
    for recipient in plan.recipients:
        recipient_context: TemplateContext = {**base_context, "recipient_name": recipient.name}
        rendered = render_notification(template, recipient_context)
        notification = Notification.objects.create(
            template_id=template.pk,
            event_id=outbox_event.pk if outbox_event else None,
            user_id=recipient.user.pk if recipient.user else None,
            external_invitation_id=recipient.external_invitation.pk if recipient.external_invitation else None,
            external_email=recipient.external_email,
            subject=rendered.subject,
            body=rendered.body_text,
            body_html=rendered.body_html,
        )
        secret_content: RenderedMessage | None = None
        if plan.secret_context:
            secret_content = render_notification(template, {**recipient_context, **plan.secret_context})
        deliveries.append(PlannedDelivery(notification, secret_content, plan.after_send))
    return deliveries


def _payload_matches_catalog(envelope: EventEnvelope) -> bool:
    """
    Check the received payload keys against the catalog.

    Args:
        envelope: Received event.

    Returns:
        bool: True when the keys are exactly the catalog keys.
    """
    expected_keys = PAYLOAD_KEYS.get(envelope.event_type)
    return expected_keys is not None and set(envelope.payload) == expected_keys


def handle_event(envelope: EventEnvelope) -> HandleOutcome:
    """
    Create and send the notifications of one event, at most once per event.

    The ``ProcessedEvent`` marker and the notifications are committed together;
    e-mails are sent only after the commit, so a redelivered message is detected
    as duplicate and never produces a second e-mail. A delivery failure is stored
    on the notification (for ``--retry-pending``) and does not reject the event.

    Args:
        envelope: Decoded event.

    Returns:
        HandleOutcome: ``PROCESSED``, ``DUPLICATE`` or ``REJECTED`` (unknown type,
            unsupported version or payload outside the catalog).
    """
    if not is_known_event(envelope.event_type, envelope.event_version) or not _payload_matches_catalog(envelope):
        logger.warning("Rejected event %s: unsupported type/version or payload (%s %s)", envelope.event_id, envelope.event_type, envelope.event_version)
        return HandleOutcome.REJECTED
    started = time.monotonic()
    marker_db = database_for_model(ProcessedEvent)
    if ProcessedEvent.objects.using(marker_db).filter(consumer_group=CONSUMER_GROUP, event_id=envelope.event_id).exists():
        logger.info("Event %s (%s) already processed; skipping", envelope.event_id, envelope.event_type)
        return HandleOutcome.DUPLICATE
    notification_db = database_for_model(Notification)
    try:
        with transaction.atomic(using=notification_db):
            try:
                plan = _plan_event(envelope)
            except EntityNotFoundError:
                logger.warning("Event %s (%s) refers to a record that no longer exists; no notification created", envelope.event_id, envelope.event_type)
                deliveries: list[PlannedDelivery] = []
            else:
                deliveries = _create_notifications(envelope, plan)
    except IntegrityError:
        return HandleOutcome.DUPLICATE
    try:
        with transaction.atomic(using=marker_db):
            marker = ProcessedEvent.objects.using(marker_db).create(consumer_group=CONSUMER_GROUP, event_id=envelope.event_id)
    except IntegrityError:
        return HandleOutcome.DUPLICATE
    marker.processing_time_ms = int((time.monotonic() - started) * 1000)
    marker.save(update_fields=["processing_time_ms"])
    sent = 0
    for delivery in deliveries:
        if not send_notification(delivery.notification, content=delivery.content):
            continue
        sent += 1
        if delivery.after_send is not None:
            delivery.after_send(delivery.notification)
    logger.info("Processed event %s (%s): %s notification(s), %s sent", envelope.event_id, envelope.event_type, len(deliveries), sent)
    return HandleOutcome.PROCESSED
