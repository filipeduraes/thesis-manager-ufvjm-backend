"""
Models for notification templates and individual deliveries.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import ExternalInvitation
from events.models import OutboxEvent


class NotificationTemplateCode(models.TextChoices):
    """
    Codes of the templates seeded by migration; one per notified event type.
    """

    THESIS_SUBMITTED = "THESIS_SUBMITTED", "Monografia submetida"
    DEFENSE_SCHEDULED = "DEFENSE_SCHEDULED", "Banca agendada"
    DEFENSE_EVALUATION_RECORDED = "DEFENSE_EVALUATION_RECORDED", "Avaliação registrada"
    DEFENSE_COMPLETED = "DEFENSE_COMPLETED", "Banca concluída"
    THESIS_DEFENSE_RESULT_RECORDED = "THESIS_DEFENSE_RESULT_RECORDED", "Resultado da banca registrado"
    THESIS_PUBLISHED = "THESIS_PUBLISHED", "Monografia publicada"
    EXTERNAL_MEMBER_INVITED = "EXTERNAL_MEMBER_INVITED", "Convite de membro externo"


class NotificationChannel(models.TextChoices):
    """
    Delivery channel of a notification.
    """

    EMAIL = "EMAIL", "E-mail"
    IN_APP = "IN_APP", "No sistema"


class NotificationStatus(models.TextChoices):
    """
    Delivery lifecycle of a notification.
    """

    PENDING = "PENDING", "Pendente"
    SENT = "SENT", "Enviada"
    DELIVERED = "DELIVERED", "Entregue"
    FAILED = "FAILED", "Falhou"
    READ = "READ", "Lida"
    # Não será reenviada: o convite foi substituído por um envio mais novo ou deixou de valer.
    CANCELLED = "CANCELLED", "Cancelada"


class NotificationTemplate(models.Model):
    """
    Subject and bodies (text and HTML) rendered with the Django template language.

    ``variables`` lists the context keys the template requires; rendering fails
    when one is missing instead of sending a message with blanks.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=50, unique=True)
    subject = models.CharField(max_length=200)
    body_html = models.TextField()
    body_text = models.TextField()
    variables = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """
        Database table and ordering for notification templates.
        """

        db_table = "notification_template"
        verbose_name = "Modelo de notificação"
        verbose_name_plural = "Modelos de notificação"
        ordering = ["code"]

    def __str__(self) -> str:
        """
        Return the template code.

        Returns:
            str: Template code.
        """
        return self.code


class Notification(models.Model):
    """
    One message to one recipient, created by the consumer from an outbox event.

    The recipient is an institutional user, an external committee member
    (invitation) or, as a fallback, a bare external address. ``subject`` and
    ``body`` keep the rendered text so a retry sends exactly the same message.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    template = models.ForeignKey(NotificationTemplate, on_delete=models.PROTECT, related_name="notifications")
    # SET_NULL para que uma futura limpeza da outbox não apague o histórico de envios.
    event = models.ForeignKey(OutboxEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    external_invitation = models.ForeignKey(ExternalInvitation, on_delete=models.SET_NULL, null=True, blank=True, related_name="notifications")
    external_email = models.EmailField(max_length=254, blank=True, default="")
    channel = models.CharField(max_length=10, choices=NotificationChannel.choices, default=NotificationChannel.EMAIL)
    subject = models.CharField(max_length=200)
    body = models.TextField()
    body_html = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=NotificationStatus.choices, default=NotificationStatus.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    priority = models.PositiveSmallIntegerField(default=5)
    scheduled_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, default="")

    class Meta:
        """
        Database table, indexes and constraints for notifications.
        """

        db_table = "notification"
        verbose_name = "Notificação"
        verbose_name_plural = "Notificações"
        ordering = ["-scheduled_at"]
        indexes = [
            models.Index(fields=["status", "scheduled_at"], name="notification_status_idx"),
            models.Index(fields=["user", "read_at"], name="notification_user_read_idx"),
        ]
        constraints = [
            # Segunda barreira contra envio duplicado (a primeira é o processed_event).
            models.UniqueConstraint(
                fields=["event", "template", "user", "external_invitation", "external_email"],
                name="notification_unique_recipient_per_event",
                condition=Q(event__isnull=False),
                nulls_distinct=False,
            ),
            models.CheckConstraint(
                condition=Q(user__isnull=False) | Q(external_invitation__isnull=False) | ~Q(external_email=""),
                name="notification_has_recipient",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the notification representation.

        Returns:
            str: Template code and status.
        """
        return f"{self.template_id} {self.status}"
