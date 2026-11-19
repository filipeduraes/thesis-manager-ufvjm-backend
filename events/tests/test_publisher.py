"""
Tests for the outbox publisher (retries, backoff and broker outages).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from events.broker import BrokerUnavailableError, EventBrokerError
from events.catalog import EventType
from events.models import OutboxEvent, OutboxStatus
from events.outbox import AggregateRef, record_event
from events.publisher import compute_retry_delay, publish_pending_events, sanitize_error
from events.tests.fakes import InMemoryEventBroker

NO_JITTER = "events.publisher.random.uniform"


def create_event(minutes_ago: int = 0) -> OutboxEvent:
    thesis_id = uuid.uuid4()
    payload = {"thesisId": str(thesis_id), "referenceCode": "TCC-2026-0001", "publishedAt": "2026-11-19T12:00:00+00:00"}
    with transaction.atomic():
        event = record_event(EventType.THESIS_PUBLISHED, AggregateRef("Thesis", thesis_id), payload)
    created_at = timezone.now() - timedelta(minutes=minutes_ago)
    OutboxEvent.objects.filter(pk=event.pk).update(created_at=created_at, next_attempt_at=created_at)
    event.refresh_from_db()
    return event


@override_settings(OUTBOX_RETRY_BASE_SECONDS=5, OUTBOX_RETRY_MAX_SECONDS=900, OUTBOX_MAX_ATTEMPTS=10)
class PublishPendingEventsTests(TestCase):
    """A batch publishes due events in order and records failures on each row."""

    def setUp(self) -> None:
        self.broker = InMemoryEventBroker()

    def test_publishes_due_events_in_creation_order(self) -> None:
        newer = create_event(minutes_ago=1)
        older = create_event(minutes_ago=5)
        summary = publish_pending_events(self.broker, batch_size=10)
        self.assertEqual(summary.published, 2)
        self.assertEqual([envelope.event_id for envelope in self.broker.published], [older.pk, newer.pk])
        for event in (older, newer):
            event.refresh_from_db()
            self.assertEqual(event.status, OutboxStatus.COMPLETED)
            self.assertIsNotNone(event.published_at)
            self.assertEqual(event.attempts, 0)

    def test_respects_batch_size(self) -> None:
        for minutes_ago in (3, 2, 1):
            create_event(minutes_ago=minutes_ago)
        summary = publish_pending_events(self.broker, batch_size=2)
        self.assertEqual(summary.published, 2)
        self.assertEqual(OutboxEvent.objects.filter(status=OutboxStatus.PENDING).count(), 1)

    def test_skips_events_not_yet_due_and_finished_events(self) -> None:
        not_due = create_event()
        OutboxEvent.objects.filter(pk=not_due.pk).update(next_attempt_at=timezone.now() + timedelta(minutes=5))
        completed = create_event()
        OutboxEvent.objects.filter(pk=completed.pk).update(status=OutboxStatus.COMPLETED)
        failed = create_event()
        OutboxEvent.objects.filter(pk=failed.pk).update(status=OutboxStatus.FAILED)
        summary = publish_pending_events(self.broker, batch_size=10)
        self.assertEqual(summary.published, 0)
        self.assertEqual(self.broker.published, [])

    def test_failure_counts_attempt_and_schedules_retry_without_blocking_batch(self) -> None:
        failing = create_event(minutes_ago=2)
        healthy = create_event(minutes_ago=1)
        self.broker.failures[failing.pk] = EventBrokerError("message nacked by broker")
        before = timezone.now()
        with patch(NO_JITTER, return_value=0.0), self.assertLogs("events.publisher", level="WARNING"):
            summary = publish_pending_events(self.broker, batch_size=10)
        self.assertEqual((summary.published, summary.retrying, summary.failed), (1, 1, 0))
        failing.refresh_from_db()
        self.assertEqual(failing.status, OutboxStatus.RETRYING)
        self.assertEqual(failing.attempts, 1)
        self.assertEqual(failing.last_error, "EventBrokerError: message nacked by broker")
        self.assertGreaterEqual(failing.next_attempt_at, before + timedelta(seconds=5))
        self.assertLessEqual(failing.next_attempt_at, timezone.now() + timedelta(seconds=5))
        self.assertIsNone(failing.failed_at)
        healthy.refresh_from_db()
        self.assertEqual(healthy.status, OutboxStatus.COMPLETED)

    def test_retry_that_succeeds_clears_last_error(self) -> None:
        event = create_event()
        OutboxEvent.objects.filter(pk=event.pk).update(status=OutboxStatus.RETRYING, attempts=2, last_error="EventBrokerError: nacked")
        publish_pending_events(self.broker, batch_size=10)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxStatus.COMPLETED)
        self.assertEqual(event.last_error, "")
        self.assertEqual(event.attempts, 2)

    def test_reaching_max_attempts_marks_failed(self) -> None:
        event = create_event()
        OutboxEvent.objects.filter(pk=event.pk).update(status=OutboxStatus.RETRYING, attempts=2, max_attempts=3)
        self.broker.failures[event.pk] = EventBrokerError("unroutable")
        with self.assertLogs("events.publisher", level="ERROR"):
            summary = publish_pending_events(self.broker, batch_size=10)
        self.assertEqual(summary.failed, 1)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxStatus.FAILED)
        self.assertEqual(event.attempts, 3)
        self.assertIsNotNone(event.failed_at)
        self.assertEqual(event.last_error, "EventBrokerError: unroutable")

    def test_broker_outage_stops_batch_without_counting_attempts(self) -> None:
        first = create_event(minutes_ago=3)
        second = create_event(minutes_ago=2)
        third = create_event(minutes_ago=1)
        self.broker.failures[second.pk] = BrokerUnavailableError("Connection reset")
        with self.assertLogs("events.publisher", level="WARNING"):
            summary = publish_pending_events(self.broker, batch_size=10)
        self.assertTrue(summary.broker_unavailable)
        self.assertEqual(summary.published, 1)
        first.refresh_from_db()
        self.assertEqual(first.status, OutboxStatus.COMPLETED)
        for event in (second, third):
            event.refresh_from_db()
            self.assertEqual(event.status, OutboxStatus.PENDING)
            self.assertEqual(event.attempts, 0)
            self.assertEqual(event.last_error, "")

    def test_logs_never_contain_payload_or_credentials(self) -> None:
        event = create_event()
        self.broker.failures[event.pk] = EventBrokerError("cannot connect to amqp://thesis_events:segredo@rabbitmq:5672/vhost")
        with self.assertLogs("events.publisher", level="INFO") as captured:
            publish_pending_events(self.broker, batch_size=10)
        output = "\n".join(captured.output)
        self.assertIn(str(event.pk), output)
        self.assertNotIn("segredo", output)
        self.assertNotIn("TCC-2026-0001", output)
        event.refresh_from_db()
        self.assertNotIn("segredo", event.last_error)


@override_settings(OUTBOX_RETRY_BASE_SECONDS=5, OUTBOX_RETRY_MAX_SECONDS=60)
class RetryDelayTests(TestCase):
    """Backoff doubles per attempt, is capped and gets bounded jitter."""

    def test_doubles_per_attempt_until_cap(self) -> None:
        with patch(NO_JITTER, return_value=0.0):
            delays = [compute_retry_delay(attempts).total_seconds() for attempts in range(1, 7)]
        self.assertEqual(delays, [5, 10, 20, 40, 60, 60])

    def test_jitter_is_at_most_ten_percent(self) -> None:
        with patch(NO_JITTER, return_value=0.0) as uniform:
            compute_retry_delay(3)
        uniform.assert_called_once_with(0, 2.0)
        for _ in range(20):
            delay_seconds = compute_retry_delay(7).total_seconds()
            self.assertGreaterEqual(delay_seconds, 60)
            self.assertLessEqual(delay_seconds, 66)


class SanitizeErrorTests(TestCase):
    """Errors stored in ``last_error`` never carry credentials and are bounded."""

    def test_redacts_broker_url_and_credentials(self) -> None:
        message = sanitize_error(ConnectionError("failed amqp://user:pass@host:5672/vh and user2:secret@host2"))
        self.assertEqual(message, "ConnectionError: failed amqp://*** and ***@host2")

    def test_truncates_long_messages(self) -> None:
        message = sanitize_error(EventBrokerError("x" * 2000))
        self.assertEqual(len(message), 500)
