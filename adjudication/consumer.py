"""
Asynchronous defense result calculation.

The evaluation endpoint only records the individual evaluation and emits
``defense.evaluation_recorded``. This consumer checks, out of the HTTP request,
whether every required member has evaluated and closes the defense through the
same domain function used by the manual close.
"""

from __future__ import annotations

import enum
import logging
import time

from django.db import IntegrityError, transaction
from django.core.exceptions import ValidationError

from events.catalog import PAYLOAD_KEYS, EventType, is_known_event
from events.envelope import EventEnvelope
from events.models import ProcessedEvent
from theses.defense_scheduling import _finalize_defense, all_required_evaluations_submitted
from theses.models import Defense, DefenseStatus

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "adjudication"
SUPPORTED_EVENT_TYPES = frozenset({EventType.DEFENSE_EVALUATION_RECORDED})


class HandleOutcome(enum.Enum):
    """
    Result of handling one event, mapped to ack/reject by the broker adapter.
    """

    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"


def _payload_matches_catalog(envelope: EventEnvelope) -> bool:
    """
    Check the received payload keys against the catalog.

    Args:
        envelope: Received event.

    Returns:
        bool: True when the keys are exactly the catalog keys.
    """
    expected_keys = PAYLOAD_KEYS.get(envelope.event_type)
    return expected_keys is not None and set(envelope.payload) == expected_keys


def close_if_ready(defense: Defense) -> bool:
    """
    Close a defense when all non-alternate evaluations have been recorded.

    Args:
        defense: Defense referenced by the event.

    Returns:
        bool: True when the defense was closed.
    """
    with transaction.atomic():
        locked = Defense.objects.select_for_update().get(pk=defense.pk)
        if locked.status != DefenseStatus.IN_PROGRESS:
            return False
        if not all_required_evaluations_submitted(locked):
            return False
        try:
            _finalize_defense(locked, closed_by=None, automatic=True)
        except ValidationError as error:
            message = error.messages[0] if error.messages else type(error).__name__
            logger.warning("Adjudication of defense %s failed: %s", defense.pk, message)
            return False
    logger.info("Defense %s closed asynchronously after the last evaluation", defense.pk)
    return True


def handle_event(envelope: EventEnvelope) -> HandleOutcome:
    """
    Process one evaluation event at most once.

    Args:
        envelope: Decoded event.

    Returns:
        HandleOutcome: Processing outcome.
    """
    if envelope.event_type not in SUPPORTED_EVENT_TYPES:
        return HandleOutcome.SKIPPED
    if not is_known_event(envelope.event_type, envelope.event_version) or not _payload_matches_catalog(envelope):
        logger.warning("Adjudication rejected event %s: unsupported version or payload", envelope.event_id)
        return HandleOutcome.REJECTED
    started = time.monotonic()
    with transaction.atomic():
        try:
            marker = ProcessedEvent.objects.create(consumer_group=CONSUMER_GROUP, event_id=envelope.event_id)
        except IntegrityError:
            logger.info("Adjudication event %s already processed; skipping", envelope.event_id)
            return HandleOutcome.DUPLICATE
        defense = Defense.objects.filter(pk=envelope.payload["defenseId"]).first()
        if defense is None:
            logger.warning("Adjudication event %s refers to a defense that no longer exists", envelope.event_id)
        else:
            close_if_ready(defense)
        marker.processing_time_ms = int((time.monotonic() - started) * 1000)
        marker.save(update_fields=["processing_time_ms"])
    return HandleOutcome.PROCESSED
