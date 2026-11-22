"""
E-mail delivery of external committee member tokens.

The token is generated here, by the consumer, never by the producer: the event
payload only carries identifiers. The invitation keeps only the token hash and the
stored ``notification`` holds a masked copy of the message; the readable token
exists only in the message handed to the e-mail backend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import AuditAction, ExternalInvitation
from accounts.services import issue_external_token, mark_external_token_sent, record_audit
from events.catalog import ExternalInvitationTrigger
from events.outbox import EventPayload
from notifications.models import Notification, NotificationStatus, NotificationTemplateCode
from notifications.plans import EntityNotFoundError, NotificationPlan
from notifications.recipients import Recipient
from notifications.rendering import RenderedMessage, TemplateContext, render_notification
from theses.models import ACTIVE_DEFENSE_STATUSES, CommitteeRole, DefenseMember

logger = logging.getLogger(__name__)

MASKED_ACCESS_TOKEN = "UFVJM-****"
DISPLAY_TIMEZONE = ZoneInfo("America/Sao_Paulo")
AUDIT_EVENT_BY_TRIGGER: dict[str, str] = {
    ExternalInvitationTrigger.CREATED: "INVITATION_EMAIL_SENT",
    ExternalInvitationTrigger.REGENERATED: "INVITATION_EMAIL_SENT",
    ExternalInvitationTrigger.RESENT: "INVITATION_EMAIL_RESENT",
}


def _format_local(value: datetime) -> str:
    """
    Format a timezone-aware datetime for Brazilian readers.

    Args:
        value: Datetime to format.

    Returns:
        str: Date and time in America/Sao_Paulo, e.g. ``05/12/2026 às 14:00``.
    """
    return value.astimezone(DISPLAY_TIMEZONE).strftime("%d/%m/%Y às %H:%M")


def lock_invitation_member(invitation_id: UUID | str) -> tuple[ExternalInvitation, DefenseMember]:
    """
    Lock an invitation and load the committee member it is bound to.

    Args:
        invitation_id: Invitation identifier.

    Returns:
        tuple[ExternalInvitation, DefenseMember]: Locked invitation and its member (with defense and thesis).

    Raises:
        EntityNotFoundError: When the invitation or its committee member no longer exists.
    """
    invitation = ExternalInvitation.objects.select_for_update().filter(pk=invitation_id).first()
    if invitation is None:
        raise EntityNotFoundError("Invitation referenced by the event was not found")
    member = DefenseMember.objects.select_related("defense__thesis").filter(external_invitation_id=invitation.pk).first()
    if member is None:
        raise EntityNotFoundError("Committee member of the invitation was not found")
    return invitation, member


def issuance_block_reason(invitation: ExternalInvitation, member: DefenseMember, event_id: UUID | None) -> str | None:
    """
    Tell why a token must not be issued for this event, if any.

    Args:
        invitation: Locked invitation.
        member: Committee member bound to the invitation.
        event_id: Outbox event that requested the e-mail.

    Returns:
        str | None: Short reason code for logs, or None when the token may be issued.
    """
    if event_id is None or invitation.token_event_id != event_id:
        return "superseded"
    if not invitation.is_usable:
        return "invitation_not_usable"
    if member.defense.status not in ACTIVE_DEFENSE_STATUSES:
        return "defense_not_active"
    return None


def invitation_context(invitation: ExternalInvitation, member: DefenseMember) -> TemplateContext:
    """
    Build the template variables of an invitation e-mail, with the token masked.

    Args:
        invitation: Invitation being e-mailed.
        member: Committee member bound to the invitation.

    Returns:
        TemplateContext: Variables besides the common ones (``access_token`` is masked).
    """
    role_label = CommitteeRole(member.role).label.lower()
    return {
        "role_label": role_label,
        "scheduled_at": _format_local(member.defense.scheduled_datetime),
        "expires_at": _format_local(invitation.expires_at),
        "access_token": MASKED_ACCESS_TOKEN,
        "action_url": f"{settings.FRONTEND_URL.rstrip('/')}/login",
    }


def record_invitation_delivery(notification: Notification, event_id: UUID, trigger: str) -> None:
    """
    Mark the token e-mail as sent and append the audit entry (identifiers only).

    Args:
        notification: Notification accepted by the e-mail backend.
        event_id: Outbox event that issued the token.
        trigger: ``ExternalInvitationTrigger`` code of the event.
    """
    invitation_id = notification.external_invitation_id
    if not mark_external_token_sent(invitation_id, event_id):
        logger.info("Invitation %s was reissued after notification %s was sent", invitation_id, notification.pk)
    member = DefenseMember.objects.filter(external_invitation_id=invitation_id).only("pk", "defense_id").first()
    metadata = {
        "event": AUDIT_EVENT_BY_TRIGGER.get(trigger, "INVITATION_EMAIL_SENT"),
        "trigger": trigger,
        "notificationId": str(notification.pk),
        "attempts": notification.attempts,
        "eventId": str(event_id),
        "defenseId": str(member.defense_id) if member else None,
        "memberId": str(member.pk) if member else None,
    }
    record_audit(action=AuditAction.INVITE, entity_type="ExternalInvitation", entity_id=str(invitation_id), entity_app_label="accounts", metadata=metadata)


def plan_external_member_invited(event_id: UUID, payload: EventPayload) -> NotificationPlan:
    """
    Issue a new token for the invitation and plan the e-mail that carries it.

    Must run inside the consumer transaction: the invitation row stays locked until
    the new hash and the masked notification are committed together. Nothing is
    issued when the event was superseded by a newer one, the invitation is no
    longer usable or the defense is no longer active.

    Args:
        event_id: Identifier of the event being handled.
        payload: ``defense.external_member_invited`` payload.

    Returns:
        NotificationPlan: Plan for the external member (no recipient when nothing is issued).

    Raises:
        EntityNotFoundError: When the invitation or its member no longer exists.
    """
    invitation, member = lock_invitation_member(payload["invitationId"])
    thesis = member.defense.thesis
    block_reason = issuance_block_reason(invitation, member, event_id)
    if block_reason is not None:
        logger.info("Event %s: invitation %s not e-mailed (%s)", event_id, invitation.pk, block_reason)
        return NotificationPlan(NotificationTemplateCode.EXTERNAL_MEMBER_INVITED, thesis, [])
    token = issue_external_token(invitation)
    recipient = Recipient(name=invitation.name, external_invitation=invitation, external_email=invitation.email)
    trigger = str(payload["trigger"])

    def after_send(notification: Notification) -> None:
        record_invitation_delivery(notification, event_id, trigger)

    context = invitation_context(invitation, member)
    return NotificationPlan(NotificationTemplateCode.EXTERNAL_MEMBER_INVITED, thesis, [recipient], context, {"access_token": token}, after_send)


@dataclass
class PreparedInvitationRetry:
    """
    Invitation e-mail ready to be resent after the new token hash was committed.

    Attributes:
        notification: Notification updated with the masked copy (or cancelled).
        content: Message with the readable token, kept only in memory; None when cancelled.
        event_id: Outbox event that authorizes the token.
        trigger: ``ExternalInvitationTrigger`` code of that event.
    """

    notification: Notification
    content: RenderedMessage | None
    event_id: UUID | None
    trigger: str

    @property
    def cancelled(self) -> bool:
        """
        Tell whether the notification was cancelled instead of prepared.

        Returns:
            bool: True when nothing must be sent.
        """
        return self.content is None


def _cancel_invitation_notification(notification: Notification, block_reason: str) -> PreparedInvitationRetry:
    """
    Mark an invitation e-mail as never to be resent.

    Args:
        notification: Locked notification.
        block_reason: Short reason code, also stored on the row.

    Returns:
        PreparedInvitationRetry: Cancelled retry.
    """
    notification.status = NotificationStatus.CANCELLED
    notification.error_message = f"Não reenviada: {block_reason}"
    notification.save(update_fields=["status", "error_message"])
    logger.info("Invitation notification %s cancelled (%s)", notification.pk, block_reason)
    return PreparedInvitationRetry(notification, None, notification.event_id, "")


def prepare_invitation_retry(notification_id: UUID, eligible: Q) -> PreparedInvitationRetry | None:
    """
    Issue a new token for a failed or stale invitation e-mail, never reusing the stored body.

    Runs in its own transaction and commits the new hash before returning, so the
    caller sends the e-mail only after the token is valid. The notification is moved
    to a fresh ``PENDING`` so another retry run skips it while it is being sent.
    Superseded events, unusable invitations and inactive defenses cancel the row.

    Args:
        notification_id: Invitation notification to retry.
        eligible: Filter that selects notifications still due for a retry.

    Returns:
        PreparedInvitationRetry | None: Prepared or cancelled retry; None when the row
            is locked by another run or no longer due.
    """
    with transaction.atomic():
        notification = Notification.objects.select_for_update(skip_locked=True, of=("self",)).select_related("template", "event").filter(eligible, pk=notification_id).first()
        if notification is None:
            return None
        outbox_event = notification.event
        if outbox_event is None or notification.external_invitation_id is None:
            return _cancel_invitation_notification(notification, "superseded")
        try:
            invitation, member = lock_invitation_member(notification.external_invitation_id)
        except EntityNotFoundError:
            return _cancel_invitation_notification(notification, "member_not_found")
        block_reason = issuance_block_reason(invitation, member, outbox_event.pk)
        if block_reason is not None:
            return _cancel_invitation_notification(notification, block_reason)
        thesis = member.defense.thesis
        context: TemplateContext = {"thesis_title": thesis.title, "reference_code": thesis.reference_code, "recipient_name": invitation.name, **invitation_context(invitation, member)}
        stored_copy = render_notification(notification.template, context)
        token = issue_external_token(invitation)
        content = render_notification(notification.template, {**context, "access_token": token})
        notification.subject = stored_copy.subject
        notification.body = stored_copy.body_text
        notification.body_html = stored_copy.body_html
        notification.status = NotificationStatus.PENDING
        notification.scheduled_at = timezone.now()
        notification.save(update_fields=["subject", "body", "body_html", "status", "scheduled_at"])
    trigger = str(outbox_event.payload.get("trigger", ""))
    return PreparedInvitationRetry(notification, content, outbox_event.pk, trigger)
