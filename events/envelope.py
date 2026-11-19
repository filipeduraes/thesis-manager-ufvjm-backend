"""
Wire format of the events exchanged through the broker.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.utils.dateparse import parse_datetime

from events.models import OutboxEvent
from events.outbox import EventPayload

ENVELOPE_CONTENT_TYPE = "application/json"


class InvalidEnvelopeError(ValueError):
    """
    Raised when a received message cannot be decoded into an ``EventEnvelope``.
    """


def _parse_uuid(raw_value: object, field_name: str) -> UUID:
    """
    Parse a required UUID field of a received envelope.

    Args:
        raw_value: Raw JSON value.
        field_name: Envelope field name, used in the error message.

    Returns:
        UUID: Parsed identifier.

    Raises:
        InvalidEnvelopeError: When the value is missing or not a UUID.
    """
    if not isinstance(raw_value, str):
        raise InvalidEnvelopeError(f"Envelope field {field_name} must be a UUID string")
    try:
        return UUID(raw_value)
    except ValueError as exc:
        raise InvalidEnvelopeError(f"Envelope field {field_name} must be a UUID string") from exc


def _parse_optional_uuid(raw_value: object, field_name: str) -> UUID | None:
    """
    Parse an optional UUID field of a received envelope.

    Args:
        raw_value: Raw JSON value or None.
        field_name: Envelope field name, used in the error message.

    Returns:
        UUID | None: Parsed identifier, or None when absent.

    Raises:
        InvalidEnvelopeError: When a value is present but is not a UUID.
    """
    if raw_value is None:
        return None
    return _parse_uuid(raw_value, field_name)


def _require_str(raw_value: object, field_name: str) -> str:
    """
    Return a required non-empty string field of a received envelope.

    Args:
        raw_value: Raw JSON value.
        field_name: Envelope field name, used in the error message.

    Returns:
        str: The value.

    Raises:
        InvalidEnvelopeError: When the value is missing, empty or not a string.
    """
    if not isinstance(raw_value, str) or not raw_value:
        raise InvalidEnvelopeError(f"Envelope field {field_name} must be a non-empty string")
    return raw_value


@dataclass(frozen=True)
class EventEnvelope:
    """
    Event as published to the broker (JSON body in camelCase).

    Attributes:
        event_id: Outbox event identifier, also used as AMQP ``message_id``.
        event_type: Catalog event type (routing key).
        event_version: Contract version of the payload.
        occurred_at: When the state change was committed to the outbox.
        aggregate_type: Model name of the aggregate.
        aggregate_id: Aggregate primary key.
        correlation_id: Identifier shared by events of the same flow.
        causation_id: Identifier of the event that caused this one, if any.
        payload: Event data (identifiers and codes only).
    """

    event_id: UUID
    event_type: str
    event_version: str
    occurred_at: datetime
    aggregate_type: str
    aggregate_id: UUID
    correlation_id: UUID | None
    causation_id: UUID | None
    payload: EventPayload

    @classmethod
    def from_outbox(cls, event: OutboxEvent) -> EventEnvelope:
        """
        Build the envelope for an outbox row.

        Args:
            event: Outbox row to publish.

        Returns:
            EventEnvelope: Envelope carrying the row data.
        """
        return cls(
            event_id=event.id,
            event_type=event.event_type,
            event_version=event.event_version,
            occurred_at=event.created_at,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            correlation_id=event.correlation_id,
            causation_id=event.causation_id,
            payload=event.payload,
        )

    def to_dict(self) -> dict[str, object]:
        """
        Serialize the envelope to its JSON-compatible camelCase form.

        Returns:
            dict[str, object]: Envelope ready for ``json.dumps``.
        """
        return {
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "eventVersion": self.event_version,
            "occurredAt": self.occurred_at.isoformat(),
            "aggregateType": self.aggregate_type,
            "aggregateId": str(self.aggregate_id),
            "correlationId": str(self.correlation_id) if self.correlation_id else None,
            "causationId": str(self.causation_id) if self.causation_id else None,
            "payload": self.payload,
        }

    def to_json_bytes(self) -> bytes:
        """
        Encode the envelope as the UTF-8 JSON message body.

        Returns:
            bytes: Message body.
        """
        envelope_dict = self.to_dict()
        return json.dumps(envelope_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json_bytes(cls, body: bytes) -> EventEnvelope:
        """
        Decode and validate a received message body.

        Only the envelope structure is validated here; whether the type and version
        are supported is decided by each consumer.

        Args:
            body: Raw message body.

        Returns:
            EventEnvelope: Decoded envelope.

        Raises:
            InvalidEnvelopeError: When the body is not valid JSON or a field is missing or malformed.
        """
        try:
            raw_envelope = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidEnvelopeError("Message body is not valid UTF-8 JSON") from exc
        if not isinstance(raw_envelope, dict):
            raise InvalidEnvelopeError("Message body must be a JSON object")
        occurred_at_raw = _require_str(raw_envelope.get("occurredAt"), "occurredAt")
        occurred_at = parse_datetime(occurred_at_raw)
        if occurred_at is None:
            raise InvalidEnvelopeError("Envelope field occurredAt must be an ISO 8601 datetime")
        payload = raw_envelope.get("payload")
        if not isinstance(payload, dict):
            raise InvalidEnvelopeError("Envelope field payload must be a JSON object")
        return cls(
            event_id=_parse_uuid(raw_envelope.get("eventId"), "eventId"),
            event_type=_require_str(raw_envelope.get("eventType"), "eventType"),
            event_version=_require_str(raw_envelope.get("eventVersion"), "eventVersion"),
            occurred_at=occurred_at,
            aggregate_type=_require_str(raw_envelope.get("aggregateType"), "aggregateType"),
            aggregate_id=_parse_uuid(raw_envelope.get("aggregateId"), "aggregateId"),
            correlation_id=_parse_optional_uuid(raw_envelope.get("correlationId"), "correlationId"),
            causation_id=_parse_optional_uuid(raw_envelope.get("causationId"), "causationId"),
            payload=payload,
        )
