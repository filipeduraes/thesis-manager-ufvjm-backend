"""
E-mail delivery of notifications through the configured ``EMAIL_BACKEND``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from notifications.models import Notification, NotificationChannel, NotificationStatus

logger = logging.getLogger(__name__)

ERROR_MESSAGE_MAX_LENGTH = 500
PENDING_RETRY_GRACE = timedelta(minutes=5)
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


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
    Describe a delivery error without e-mail addresses (SMTP errors often echo them).

    Args:
        error: Exception raised by the e-mail backend.

    Returns:
        str: Redacted and truncated error description.
    """
    message = f"{type(error).__name__}: {error}"
    message = _EMAIL_PATTERN.sub("***@***", message)
    return message[:ERROR_MESSAGE_MAX_LENGTH]


def send_notification(notification: Notification) -> bool:
    """
    Send one e-mail notification and record the outcome on the row.

    Each recipient gets an individual message, so addresses are never exposed to
    other recipients.

    Args:
        notification: Notification in ``PENDING`` or ``FAILED`` status.

    Returns:
        bool: True when the backend accepted the message.
    """
    address = notification_address(notification)
    message = EmailMultiAlternatives(subject=notification.subject, body=notification.body, from_email=settings.DEFAULT_FROM_EMAIL, to=[address])
    if notification.body_html:
        message.attach_alternative(notification.body_html, "text/html")
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
    """

    sent: int = 0
    failed: int = 0


def retry_pending_notifications(limit: int) -> RetrySummary:
    """
    Resend e-mail notifications left ``FAILED`` or stuck in ``PENDING`` (manual operation).

    ``PENDING`` rows younger than ``PENDING_RETRY_GRACE`` are skipped because the
    consumer may be sending them right now; rows are locked with ``SKIP LOCKED``
    so two retry runs never send the same message.

    Args:
        limit: Maximum number of notifications in this run.

    Returns:
        RetrySummary: Counters for the run.
    """
    summary = RetrySummary()
    now = timezone.now()
    stale_pending = Q(status=NotificationStatus.PENDING, scheduled_at__lte=now - PENDING_RETRY_GRACE)
    failed = Q(status=NotificationStatus.FAILED, scheduled_at__lte=now)
    with transaction.atomic():
        candidates = list(Notification.objects.select_for_update(skip_locked=True, of=("self",)).filter(stale_pending | failed, channel=NotificationChannel.EMAIL).order_by("scheduled_at")[:limit])
        from accounts.models import ExternalInvitation, User

        user_ids = [n.user_id for n in candidates if n.user_id]
        emails = {u.pk: u.email for u in User.objects.filter(pk__in=user_ids)}
        invitation_ids = [n.external_invitation_id for n in candidates if n.external_invitation_id]
        external_emails = {i.pk: i.email for i in ExternalInvitation.objects.filter(pk__in=invitation_ids)}
        for notification in candidates:
            notification._resolved_email = emails.get(notification.user_id) or external_emails.get(notification.external_invitation_id) or ""
            if send_notification(notification):
                summary.sent += 1
            else:
                summary.failed += 1
    logger.info("Notification retry finished: %s sent, %s failed", summary.sent, summary.failed)
    return summary
