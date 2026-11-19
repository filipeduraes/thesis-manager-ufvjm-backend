"""
Tests for the outbox models, the event catalog and ``record_event``.
"""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from events.catalog import CURRENT_VERSIONS, PAYLOAD_KEYS, EventType, is_known_event
from events.models import OutboxEvent, OutboxStatus, ProcessedEvent
from events.outbox import AggregateRef, InvalidEventError, record_event


def published_payload() -> dict[str, str]:
    return {"thesisId": str(uuid.uuid4()), "referenceCode": "TCC-2026-0001", "publishedAt": "2026-11-19T12:00:00+00:00"}


class EventCatalogTests(TestCase):
    """The catalog declares keys and a version for every event type."""

    def test_every_event_type_has_payload_keys_and_version(self) -> None:
        for event_type in EventType.values:
            self.assertIn(event_type, PAYLOAD_KEYS)
            self.assertEqual(CURRENT_VERSIONS[event_type], "1.0")

    def test_is_known_event_checks_type_and_version(self) -> None:
        self.assertTrue(is_known_event("defense.completed", "1.0"))
        self.assertFalse(is_known_event("defense.completed", "2.0"))
        self.assertFalse(is_known_event("defense.unknown", "1.0"))

    def test_payload_keys_never_include_personal_or_free_text_fields(self) -> None:
        forbidden = {"email", "name", "notes", "comments", "reason", "location", "token", "score", "decision"}
        for event_type, keys in PAYLOAD_KEYS.items():
            lowered = {key.lower() for key in keys}
            self.assertFalse(lowered & forbidden, event_type)


class RecordEventTests(TestCase):
    """``record_event`` persists a pending row validated against the catalog."""

    def setUp(self) -> None:
        self.aggregate = AggregateRef("Thesis", uuid.uuid4())

    @override_settings(OUTBOX_MAX_ATTEMPTS=3)
    def test_records_pending_event_with_contract_defaults(self) -> None:
        payload = published_payload()
        with transaction.atomic():
            event = record_event(EventType.THESIS_PUBLISHED, self.aggregate, payload)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxStatus.PENDING)
        self.assertEqual(event.event_type, "thesis.published")
        self.assertEqual(event.event_version, "1.0")
        self.assertEqual(event.aggregate_type, "Thesis")
        self.assertEqual(event.aggregate_id, self.aggregate.aggregate_id)
        self.assertEqual(event.payload, payload)
        self.assertEqual(event.attempts, 0)
        self.assertEqual(event.max_attempts, 3)
        self.assertEqual(event.correlation_id, event.id)
        self.assertIsNone(event.causation_id)
        self.assertEqual(event.headers, {"contentType": "application/json", "source": "thesis-manager-backend"})
        self.assertIsNotNone(event.next_attempt_at)
        self.assertIsNone(event.published_at)
        self.assertEqual(event.last_error, "")

    def test_rejects_unexpected_key(self) -> None:
        payload = published_payload()
        payload["email"] = "aluno@ufvjm.edu.br"
        with transaction.atomic(), self.assertRaises(InvalidEventError):
            record_event(EventType.THESIS_PUBLISHED, self.aggregate, payload)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_rejects_missing_key(self) -> None:
        payload = published_payload()
        del payload["referenceCode"]
        with transaction.atomic(), self.assertRaises(InvalidEventError):
            record_event(EventType.THESIS_PUBLISHED, self.aggregate, payload)

    def test_rejects_nested_values(self) -> None:
        payload: dict = published_payload()
        payload["referenceCode"] = {"code": "TCC"}
        with transaction.atomic(), self.assertRaises(InvalidEventError):
            record_event(EventType.THESIS_PUBLISHED, self.aggregate, payload)

    def test_rejects_unknown_event_type(self) -> None:
        with transaction.atomic(), self.assertRaises(InvalidEventError):
            record_event("thesis.unknown", self.aggregate, {})

    def test_event_is_rolled_back_with_the_transaction(self) -> None:
        with self.assertRaises(RuntimeError), transaction.atomic():
            record_event(EventType.THESIS_PUBLISHED, self.aggregate, published_payload())
            raise RuntimeError("falha na transição")
        self.assertFalse(OutboxEvent.objects.exists())


class RecordEventOutsideTransactionTests(TransactionTestCase):
    """Without an atomic block the event could outlive a failed transition."""

    def test_requires_atomic_block(self) -> None:
        aggregate = AggregateRef("Thesis", uuid.uuid4())
        with self.assertRaises(RuntimeError):
            record_event(EventType.THESIS_PUBLISHED, aggregate, published_payload())
        self.assertFalse(OutboxEvent.objects.exists())


class ProcessedEventTests(TestCase):
    """The composite key rejects a second marker for the same consumer and event."""

    def test_duplicate_marker_raises_integrity_error(self) -> None:
        event_id = uuid.uuid4()
        ProcessedEvent.objects.create(consumer_group="notifications", event_id=event_id)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProcessedEvent.objects.create(consumer_group="notifications", event_id=event_id)

    def test_same_event_can_be_processed_by_other_consumer_group(self) -> None:
        event_id = uuid.uuid4()
        ProcessedEvent.objects.create(consumer_group="notifications", event_id=event_id)
        ProcessedEvent.objects.create(consumer_group="search-index", event_id=event_id)
        self.assertEqual(ProcessedEvent.objects.filter(event_id=event_id).count(), 2)
