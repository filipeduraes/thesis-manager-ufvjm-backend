"""
Models for the transactional outbox and consumer-side idempotency.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone

from events.catalog import EVENT_VERSION_1_0


class OutboxStatus(models.TextChoices):
    """
    Outbox event lifecycle; mirrors the ``processing_status`` enum of the data model.

    ``COMPLETED`` means the broker confirmed the publication.
    """

    PENDING = "PENDING", "Pendente"
    PROCESSING = "PROCESSING", "Processando"
    COMPLETED = "COMPLETED", "Publicado"
    FAILED = "FAILED", "Falhou"
    RETRYING = "RETRYING", "Reprocessando"


PUBLISHABLE_OUTBOX_STATUSES: tuple[str, ...] = (OutboxStatus.PENDING, OutboxStatus.RETRYING)


def default_max_attempts() -> int:
    """
    Return the configured maximum number of publication attempts.

    Returns:
        int: Value of ``settings.OUTBOX_MAX_ATTEMPTS``.
    """
    return settings.OUTBOX_MAX_ATTEMPTS


def default_event_headers() -> dict[str, str]:
    """
    Return the default transport headers stored with every outbox event.

    Returns:
        dict[str, str]: Content type and producing service.
    """
    return {"contentType": "application/json", "source": "thesis-manager-backend"}


class OutboxEvent(models.Model):
    """
    Domain event written in the same transaction as the state change that caused it.

    A separate publisher reads pending rows, sends them to the broker and records
    the outcome, so no broker call ever happens inside the HTTP request.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    aggregate_type = models.CharField(max_length=50)
    aggregate_id = models.UUIDField()
    event_type = models.CharField(max_length=100)
    event_version = models.CharField(max_length=10, default=EVENT_VERSION_1_0)
    payload = models.JSONField()
    headers = models.JSONField(default=default_event_headers)
    status = models.CharField(max_length=20, choices=OutboxStatus.choices, default=OutboxStatus.PENDING)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=default_max_attempts)
    correlation_id = models.UUIDField(null=True, blank=True)
    causation_id = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    # Campo além da modelagem original: agenda a próxima tentativa (backoff).
    next_attempt_at = models.DateTimeField(default=timezone.now)
    published_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    class Meta:
        """
        Database table, ordering, indexes and constraints for outbox events.
        """

        db_table = "outbox_event"
        verbose_name = "Evento da outbox"
        verbose_name_plural = "Eventos da outbox"
        ordering = ["created_at"]
        indexes = [
            models.Index(
                fields=["next_attempt_at", "created_at"],
                name="outbox_event_due_idx",
                condition=Q(status__in=PUBLISHABLE_OUTBOX_STATUSES),
            ),
            models.Index(fields=["aggregate_type", "aggregate_id"], name="outbox_event_aggregate_idx"),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(max_attempts__gte=1), name="outbox_event_max_attempts_gte_1"),
        ]

    def __str__(self) -> str:
        """
        Return the event representation used in the admin and in debugging.

        Returns:
            str: Event type and identifier.
        """
        return f"{self.event_type} {self.pk}"


class ProcessedEvent(models.Model):
    """
    Marker of an event already handled by a consumer group (idempotent consumption).

    The composite primary key makes a second insert for the same delivery fail,
    which is how a redelivered message is detected.
    """

    pk = models.CompositePrimaryKey("consumer_group", "event_id")
    consumer_group = models.CharField(max_length=100)
    event_id = models.UUIDField()
    processed_at = models.DateTimeField(default=timezone.now)
    processing_time_ms = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        """
        Database table for processed event markers.
        """

        db_table = "processed_event"
        verbose_name = "Evento processado"
        verbose_name_plural = "Eventos processados"

    def __str__(self) -> str:
        """
        Return the marker representation.

        Returns:
            str: Consumer group and event identifier.
        """
        return f"{self.consumer_group}:{self.event_id}"
