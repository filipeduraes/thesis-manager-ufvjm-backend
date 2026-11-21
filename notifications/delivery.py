"""
E-mail delivery of notifications through the configured ``EMAIL_BACKEND``.
"""

from __future__ import annotations

import logging
import re

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from notifications.models import Notification, NotificationStatus

logger = logging.getLogger(__name__)

ERROR_MESSAGE_MAX_LENGTH = 500
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
    if notification.user is not None:
        return notification.user.email
    return notification.external_invitation.email


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
