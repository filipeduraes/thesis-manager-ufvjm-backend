"""
Tests for the broker wire format (``EventEnvelope``).
"""

from __future__ import annotations

import json
import uuid

from django.db import transaction
from django.test import TestCase

from events.catalog import EventType
from events.envelope import EventEnvelope, InvalidEnvelopeError
from events.outbox import AggregateRef, record_event


class EventEnvelopeTests(TestCase):
    """Envelopes round-trip through JSON and reject malformed bodies."""

    def setUp(self) -> None:
        self.thesis_id = uuid.uuid4()
        payload = {"thesisId": str(self.thesis_id), "versionNumber": 2, "submittedAt": "2026-11-19T12:00:00+00:00"}
        with transaction.atomic():
            self.event = record_event(EventType.THESIS_SUBMITTED, AggregateRef("Thesis", self.thesis_id), payload)

    def test_to_dict_uses_camel_case_contract(self) -> None:
        envelope = EventEnvelope.from_outbox(self.event)
        envelope_dict = envelope.to_dict()
        self.assertEqual(
            set(envelope_dict),
            {"eventId", "eventType", "eventVersion", "occurredAt", "aggregateType", "aggregateId", "correlationId", "causationId", "payload"},
        )
        self.assertEqual(envelope_dict["eventId"], str(self.event.id))
        self.assertEqual(envelope_dict["eventType"], "thesis.submitted")
        self.assertEqual(envelope_dict["correlationId"], str(self.event.id))
        self.assertIsNone(envelope_dict["causationId"])

    def test_json_round_trip(self) -> None:
        envelope = EventEnvelope.from_outbox(self.event)
        body = envelope.to_json_bytes()
        decoded = EventEnvelope.from_json_bytes(body)
        self.assertEqual(decoded, envelope)

    def test_rejects_invalid_json(self) -> None:
        with self.assertRaises(InvalidEnvelopeError):
            EventEnvelope.from_json_bytes(b"{not json")

    def test_rejects_non_object_body(self) -> None:
        with self.assertRaises(InvalidEnvelopeError):
            EventEnvelope.from_json_bytes(b"[]")

    def test_rejects_missing_or_malformed_fields(self) -> None:
        valid = EventEnvelope.from_outbox(self.event).to_dict()
        broken_variants = [
            {**valid, "eventId": "not-a-uuid"},
            {**valid, "eventType": ""},
            {**valid, "occurredAt": "ontem"},
            {**valid, "payload": ["thesisId"]},
            {key: value for key, value in valid.items() if key != "aggregateId"},
        ]
        for variant in broken_variants:
            body = json.dumps(variant).encode("utf-8")
            with self.subTest(variant=variant), self.assertRaises(InvalidEnvelopeError):
                EventEnvelope.from_json_bytes(body)
