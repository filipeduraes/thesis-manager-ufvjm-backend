"""
Producer-side API to record domain events in the transactional outbox.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from django.db import transaction

from events.catalog import CURRENT_VERSIONS, PAYLOAD_KEYS, EventType
from events.models import OutboxEvent

logger = logging.getLogger(__name__)

PayloadValue = str | int | bool | None
EventPayload = dict[str, PayloadValue]


class InvalidEventError(ValueError):
    """
    Raised when an event does not match the contract declared in ``events.catalog``.
    """


@dataclass(frozen=True)
class AggregateRef:
    """
    Reference to the aggregate whose state change produced the event.

    Attributes:
        aggregate_type: Model name of the aggregate, e.g. ``"Thesis"`` or ``"Defense"``.
        aggregate_id: Primary key of the aggregate.
    """

    aggregate_type: str
    aggregate_id: UUID


def _validate_payload(event_type: str, payload: EventPayload) -> None:
    """
    Check that the payload has exactly the catalog keys and only flat JSON scalars.

    Args:
        event_type: Event type code.
        payload: Payload to validate.

    Raises:
        InvalidEventError: When the type is unknown, keys differ from the catalog,
            or a value is not a string, integer, boolean or null.
    """
    expected_keys = PAYLOAD_KEYS.get(event_type)
    if expected_keys is None:
        raise InvalidEventError(f"Unknown event type: {event_type}")
    received_keys = set(payload)
    if received_keys != expected_keys:
        missing = sorted(expected_keys - received_keys)
        unexpected = sorted(received_keys - expected_keys)
        raise InvalidEventError(f"Payload keys for {event_type} do not match the catalog (missing={missing}, unexpected={unexpected})")
    for key, value in payload.items():
        if value is not None and not isinstance(value, (str, int, bool)):
            raise InvalidEventError(f"Payload value for {event_type}.{key} must be a JSON scalar")


def record_event(event_type: EventType, aggregate: AggregateRef, payload: EventPayload) -> OutboxEvent:
    """
    Record a domain event in the outbox as part of the caller's transaction.

    Must be called inside ``transaction.atomic`` together with the state change it
    describes, so the event exists if and only if that change is committed.

    Args:
        event_type: Catalog event type.
        aggregate: Aggregate that changed.
        payload: Event data restricted to identifiers and codes.

    Returns:
        OutboxEvent: Persisted outbox row in ``PENDING`` status.

    Raises:
        RuntimeError: When called outside an atomic block.
        InvalidEventError: When the payload does not match the catalog.

    Examples:
        >>> with transaction.atomic():
        ...     record_event(EventType.THESIS_PUBLISHED, AggregateRef("Thesis", thesis.pk), payload)
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("record_event must run inside transaction.atomic together with the state change")
    _validate_payload(event_type, payload)
    event = OutboxEvent(
        aggregate_type=aggregate.aggregate_type,
        aggregate_id=aggregate.aggregate_id,
        event_type=event_type,
        event_version=CURRENT_VERSIONS[event_type],
        payload=payload,
    )
    # Evento raiz: a correlação começa nele mesmo; nunca vem de cabeçalho do cliente.
    event.correlation_id = event.id
    event.save()
    logger.debug("Recorded outbox event %s (%s)", event.pk, event.event_type)
    return event
