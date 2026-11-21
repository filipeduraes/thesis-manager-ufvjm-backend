"""
Outbox publisher: sends pending events to the broker with retries and exponential backoff.
"""

from __future__ import annotations

import logging
import random
import re
import threading
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from events.broker import BrokerUnavailableError, EventBroker, EventBrokerError
from events.envelope import EventEnvelope
from events.models import PUBLISHABLE_OUTBOX_STATUSES, OutboxEvent, OutboxStatus

logger = logging.getLogger(__name__)

LAST_ERROR_MAX_LENGTH = 500
JITTER_RATIO = 0.1
BROKER_RECONNECT_MAX_SECONDS = 60.0
_BROKER_URL_PATTERN = re.compile(r"amqps?://\S+", re.IGNORECASE)
_CREDENTIALS_PATTERN = re.compile(r"[^\s/@:]+:[^\s/@]+@")


@dataclass
class PublishSummary:
    """
    Outcome counters of one publisher batch.

    Attributes:
        published: Events confirmed by the broker.
        retrying: Events that failed and were rescheduled.
        failed: Events that exhausted ``max_attempts``.
        broker_unavailable: Whether the batch stopped because the broker was unreachable.
    """

    published: int = 0
    retrying: int = 0
    failed: int = 0
    broker_unavailable: bool = False


def sanitize_error(error: BaseException) -> str:
    """
    Describe a publication error without credentials or connection URLs.

    Args:
        error: Exception raised by the broker client.

    Returns:
        str: Exception class and message, redacted and truncated for ``last_error``.
    """
    message = f"{type(error).__name__}: {error}"
    message = _BROKER_URL_PATTERN.sub("amqp://***", message)
    message = _CREDENTIALS_PATTERN.sub("***@", message)
    return message[:LAST_ERROR_MAX_LENGTH]


def compute_retry_delay(attempts: int) -> timedelta:
    """
    Compute the exponential backoff before the next attempt, with jitter.

    The base delay doubles at each attempt (``base * 2 ** (attempts - 1)``), is capped
    at ``OUTBOX_RETRY_MAX_SECONDS`` and receives up to 10% of random jitter so that
    events that failed together are not retried in lockstep.

    Args:
        attempts: Number of failed attempts so far (at least 1).

    Returns:
        timedelta: Delay until the next attempt.

    Examples:
        With base 5 s: 5 s, 10 s, 20 s, 40 s, ... up to the cap (plus jitter).
    """
    exponent = max(attempts - 1, 0)
    base_seconds = settings.OUTBOX_RETRY_BASE_SECONDS * (2 ** exponent)
    capped_seconds = min(base_seconds, settings.OUTBOX_RETRY_MAX_SECONDS)
    jitter_seconds = random.uniform(0, capped_seconds * JITTER_RATIO)
    return timedelta(seconds=capped_seconds + jitter_seconds)


def _mark_published(event: OutboxEvent) -> None:
    """
    Record a confirmed publication.

    Args:
        event: Locked outbox row.
    """
    event.status = OutboxStatus.COMPLETED
    event.published_at = timezone.now()
    event.last_error = ""
    event.save(update_fields=["status", "published_at", "last_error"])
    logger.info("Published outbox event %s (%s)", event.pk, event.event_type)


def _mark_failed_attempt(event: OutboxEvent, error: EventBrokerError) -> str:
    """
    Count a failed attempt and either schedule a retry or give up.

    Args:
        event: Locked outbox row.
        error: Error raised for this message.

    Returns:
        str: Resulting status (``RETRYING`` or ``FAILED``).
    """
    now = timezone.now()
    event.attempts += 1
    event.last_error = sanitize_error(error)
    if event.attempts >= event.max_attempts:
        event.status = OutboxStatus.FAILED
        event.failed_at = now
        logger.error("Outbox event %s (%s) failed after %s attempts", event.pk, event.event_type, event.attempts)
    else:
        event.status = OutboxStatus.RETRYING
        retry_delay = compute_retry_delay(event.attempts)
        event.next_attempt_at = now + retry_delay
        logger.warning("Outbox event %s (%s) publication failed (attempt %s/%s); retrying in %.0f s", event.pk, event.event_type, event.attempts, event.max_attempts, retry_delay.total_seconds())
    event.save(update_fields=["attempts", "last_error", "status", "failed_at", "next_attempt_at"])
    return event.status


def publish_pending_events(broker: EventBroker, batch_size: int) -> PublishSummary:
    """
    Publish one batch of due outbox events.

    Rows are locked with ``SKIP LOCKED`` so several publishers can run without
    sending the same event concurrently. Delivery is at-least-once: if the commit
    fails after a confirm, the event is sent again and consumers deduplicate it
    by ``eventId``.

    Args:
        broker: Broker client that confirms each publication.
        batch_size: Maximum number of events in this batch.

    Returns:
        PublishSummary: Counters for the batch.
    """
    summary = PublishSummary()
    with transaction.atomic():
        due_events = OutboxEvent.objects.select_for_update(skip_locked=True).filter(status__in=PUBLISHABLE_OUTBOX_STATUSES, next_attempt_at__lte=timezone.now()).order_by("created_at", "id")[:batch_size]
        for event in due_events:
            envelope = EventEnvelope.from_outbox(event)
            try:
                broker.publish(envelope)
            except BrokerUnavailableError:
                summary.broker_unavailable = True
                logger.warning("Event broker unavailable; outbox batch interrupted at event %s", event.pk)
                break
            except EventBrokerError as error:
                resulting_status = _mark_failed_attempt(event, error)
                if resulting_status == OutboxStatus.FAILED:
                    summary.failed += 1
                else:
                    summary.retrying += 1
                continue
            _mark_published(event)
            summary.published += 1
    return summary


def run_publisher_loop(broker: EventBroker, stop_event: threading.Event, batch_size: int, poll_interval: float) -> None:
    """
    Publish batches until ``stop_event`` is set.

    A full batch is followed immediately by the next one; otherwise the loop waits
    ``poll_interval``. While the broker is unreachable the wait doubles up to
    ``BROKER_RECONNECT_MAX_SECONDS``, without touching event attempts.

    Args:
        broker: Broker client used for every batch.
        stop_event: Set by the signal handler to stop after the current batch.
        batch_size: Maximum number of events per batch.
        poll_interval: Seconds to wait when there is nothing more to publish.
    """
    unavailable_streak = 0
    while not stop_event.is_set():
        summary = publish_pending_events(broker, batch_size)
        if summary.broker_unavailable:
            unavailable_streak += 1
            reconnect_delay = min(poll_interval * (2 ** unavailable_streak), BROKER_RECONNECT_MAX_SECONDS)
            stop_event.wait(reconnect_delay)
            continue
        if unavailable_streak:
            logger.info("Event broker reachable again after %s failed batch(es)", unavailable_streak)
            unavailable_streak = 0
        processed = summary.published + summary.retrying + summary.failed
        if processed >= batch_size:
            continue
        stop_event.wait(poll_interval)


def requeue_failed_events(event_id: UUID | None) -> int:
    """
    Put ``FAILED`` events back in the queue after the cause was fixed (manual operation).

    Attempts are reset; ``last_error`` is kept for diagnosis until the next outcome.

    Args:
        event_id: Single event to requeue, or None for every failed event.

    Returns:
        int: Number of requeued events.
    """
    failed_events = OutboxEvent.objects.filter(status=OutboxStatus.FAILED)
    if event_id is not None:
        failed_events = failed_events.filter(pk=event_id)
    requeued = failed_events.update(status=OutboxStatus.PENDING, attempts=0, failed_at=None, next_attempt_at=timezone.now())
    logger.info("Requeued %s failed outbox event(s)", requeued)
    return requeued
