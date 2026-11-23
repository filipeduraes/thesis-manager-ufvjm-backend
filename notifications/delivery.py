"""
E-mail delivery of notifications through the configured ``EMAIL_BACKEND``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction

from accounts.concurrency import database_for_model
from django.db.models import Q
from django.utils import timezone

from notifications.invitations import prepare_invitation_retry, record_invitation_delivery
from notifications.models import Notification, NotificationChannel, NotificationStatus, NotificationTemplateCode
from notifications.rendering import RenderedMessage

logger = logging.getLogger(__name__)

ERROR_MESSAGE_MAX_LENGTH = 500
PENDING_RETRY_GRACE = timedelta(minutes=5)
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# Alguns backends ecoam trechos da mensagem no erro; o token do convite nunca pode ir para o banco.
_ACCESS_TOKEN_PATTERN = re.compile(r"UFVJM(?:-[A-Z0-9]{4}){4}", re.IGNORECASE)


def notification_address(notification: Notification) -> str:
    """
    Return the address that receives a notification.

    Args:
        notification: Notification with its recipient relations.

    Returns:
        str: E-mail address (external address first, then the user's).
    """
    if notification.external_email:
        return notification.external_email
    resolved = getattr(notification, "_resolved_email", None)
    if resolved:
        return resolved
    if notification.user_id:
        from accounts.models import User

        return User.objects.filter(pk=notification.user_id).values_list("email", flat=True).first() or ""
    if notification.external_invitation_id:
        from accounts.models import ExternalInvitation

        return ExternalInvitation.objects.filter(pk=notification.external_invitation_id).values_list("email", flat=True).first() or ""
    return ""


def sanitize_delivery_error(error: BaseException) -> str:
    """
    Describe a delivery error without e-mail addresses or access tokens (SMTP errors often echo them).

    Args:
        error: Exception raised by the e-mail backend.

    Returns:
        str: Redacted and truncated error description.
    """
    message = f"{type(error).__name__}: {error}"
    message = _EMAIL_PATTERN.sub("***@***", message)
    message = _ACCESS_TOKEN_PATTERN.sub("UFVJM-****", message)
    return message[:ERROR_MESSAGE_MAX_LENGTH]


def send_notification(notification: Notification, content: RenderedMessage | None = None) -> bool:
    """
    Send one e-mail notification and record the outcome on the row.

    Each recipient gets an individual message, so addresses are never exposed to
    other recipients.

    Args:
        notification: Notification in ``PENDING`` or ``FAILED`` status.
        content: Message to send instead of the stored copy, used when the real
            message carries a secret (the invitation token) that the row must not keep.

    Returns:
        bool: True when the backend accepted the message.
    """
    address = notification_address(notification)
    message_content = content or RenderedMessage(subject=notification.subject, body_text=notification.body, body_html=notification.body_html)
    message = EmailMultiAlternatives(subject=message_content.subject, body=message_content.body_text, from_email=settings.DEFAULT_FROM_EMAIL, to=[address])
    if message_content.body_html:
        message.attach_alternative(message_content.body_html, "text/html")
    notification.attempts += 1
    try:
        message.send()
    # Qualquer backend de e-mail pode falhar de formas diferentes (SMTP, rede, API);
    # a falha é registrada na linha para reenvio manual, sem derrubar o consumidor.
    except Exception as error:
        notification.status = NotificationStatus.FAILED
        notification.failed_at = timezone.now()
        notification.error_message = sanitize_delivery_error(error)
        notification.save(update_fields=["status", "failed_at", "error_message", "attempts"])
        # Sem traceback: mensagens de erro SMTP costumam repetir o endereço do destinatário.
        logger.error("Notification %s delivery failed (attempt %s): %s", notification.pk, notification.attempts, type(error).__name__)
        return False
    notification.status = NotificationStatus.SENT
    notification.sent_at = timezone.now()
    notification.error_message = ""
    notification.save(update_fields=["status", "sent_at", "error_message", "attempts"])
    logger.info("Notification %s sent (template=%s, attempt %s)", notification.pk, notification.template_id, notification.attempts)
    return True


@dataclass
class RetrySummary:
    """
    Outcome counters of a manual retry run.

    Attributes:
        sent: Notifications delivered in this run.
        failed: Notifications that failed again.
        cancelled: Invitation e-mails cancelled because the invitation was reissued or is no longer valid.
    """

    sent: int = 0
    failed: int = 0
    cancelled: int = 0


def _retry_eligible(now: datetime) -> Q:
    """
    Build the filter of notifications due for a manual retry.

    Args:
        now: Reference time of the run.

    Returns:
        Q: Failed rows, and pending rows older than ``PENDING_RETRY_GRACE``, on the e-mail channel.
    """
    stale_pending = Q(status=NotificationStatus.PENDING, scheduled_at__lte=now - PENDING_RETRY_GRACE)
    failed = Q(status=NotificationStatus.FAILED, scheduled_at__lte=now)
    return (stale_pending | failed) & Q(channel=NotificationChannel.EMAIL)


def _retry_invitation(notification_id: UUID, eligible: Q, summary: RetrySummary) -> None:
    """
    Resend an invitation e-mail with a newly issued token, never the stored body.

    Args:
        notification_id: Invitation notification.
        eligible: Filter of notifications still due.
        summary: Counters updated in place.
    """
    prepared = prepare_invitation_retry(notification_id, eligible)
    if prepared is None:
        return
    if prepared.cancelled:
        summary.cancelled += 1
        return
    if not send_notification(prepared.notification, content=prepared.content):
        summary.failed += 1
        return
    summary.sent += 1
    record_invitation_delivery(prepared.notification, prepared.event_id, prepared.trigger)


def retry_pending_notifications(limit: int) -> RetrySummary:
    """
    Resend e-mail notifications left ``FAILED`` or stuck in ``PENDING`` (manual operation).

    ``PENDING`` rows younger than ``PENDING_RETRY_GRACE`` are skipped because the
    consumer may be sending them right now; rows are locked with ``SKIP LOCKED``
    so two retry runs never send the same message. Invitation e-mails are never
    resent from the stored (masked) body: each one gets a new token, committed in
    its own transaction before the message is sent.

    Args:
        limit: Maximum number of notifications in this run.

    Returns:
        RetrySummary: Counters for the run.
    """
    summary = RetrySummary()
    eligible = _retry_eligible(timezone.now())
    invitation_ids: list[UUID] = []
    notification_db = database_for_model(Notification)
    with transaction.atomic(using=notification_db):
        # Apenas "template" pertence ao banco das notificações; user e
        # external_invitation ficam no banco de identidade e são resolvidos à
        # parte por notification_address() (JOIN cross-DB não é possível).
        candidates = Notification.objects.using(notification_db).select_for_update(skip_locked=True, of=("self",)).select_related("template").filter(eligible).order_by("scheduled_at")[:limit]
        for notification in candidates:
            if notification.template.code == NotificationTemplateCode.EXTERNAL_MEMBER_INVITED:
                invitation_ids.append(notification.pk)
                continue
            if send_notification(notification):
                summary.sent += 1
            else:
                summary.failed += 1
    for notification_id in invitation_ids:
        _retry_invitation(notification_id, eligible, summary)
    logger.info("Notification retry finished: %s sent, %s failed, %s cancelled", summary.sent, summary.failed, summary.cancelled)
    return summary
