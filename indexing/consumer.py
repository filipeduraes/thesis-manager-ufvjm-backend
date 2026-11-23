"""
Idempotent handler that extracts PDF text and refreshes the search index.

This consumer belongs to the indexing service: it never sends e-mails and never
writes notifications. Its only side effects are the extracted text of a stored
PDF, the denormalized search document of the theses using that file, and the
``ProcessedEvent`` marker that guarantees at-most-once processing.
"""

from __future__ import annotations

import enum
import logging
import time

from django.db import IntegrityError, transaction

from events.catalog import PAYLOAD_KEYS, EventType, is_known_event
from events.envelope import EventEnvelope
from events.models import ProcessedEvent
from events.outbox import EventPayload
from theses.models import StoredFile
from theses.tasks import extract_stored_file_text

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "indexing"
SUPPORTED_EVENT_TYPES = frozenset({EventType.THESIS_TEXT_EXTRACTION_REQUESTED})


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


def handle_event(envelope: EventEnvelope) -> HandleOutcome:
    """
    Process one indexing event at most once.

    A missing file is a recoverable no-op (the aggregate may have been removed
    after the event was recorded): the marker is still committed so the message
    is acked and does not loop. Extraction failures propagate so the adapter can
    dead-letter the message without losing the marker transaction.

    Args:
        envelope: Decoded event.

    Returns:
        HandleOutcome: Processing outcome.

    Raises:
        Exception: Propagates extraction failures for the adapter to handle.
    """
    if envelope.event_type not in SUPPORTED_EVENT_TYPES:
        return HandleOutcome.SKIPPED
    if not is_known_event(envelope.event_type, envelope.event_version) or not _payload_matches_catalog(envelope):
        logger.warning("Indexing rejected event %s: unsupported version or payload", envelope.event_id)
        return HandleOutcome.REJECTED
    started = time.monotonic()
    with transaction.atomic():
        try:
            ProcessedEvent.objects.create(consumer_group=CONSUMER_GROUP, event_id=envelope.event_id)
        except IntegrityError:
            logger.info("Indexing event %s already processed; skipping", envelope.event_id)
            return HandleOutcome.DUPLICATE
        stored_file = StoredFile.objects.filter(pk=envelope.payload["storedFileId"]).first()
        if stored_file is None:
            logger.warning("Indexing event %s refers to a stored file that no longer exists", envelope.event_id)
        else:
            extract_stored_file_text(stored_file)
            logger.info("Indexing event %s extracted text of stored file %s", envelope.event_id, stored_file.pk)
        marker = ProcessedEvent.objects.get(consumer_group=CONSUMER_GROUP, event_id=envelope.event_id)
        marker.processing_time_ms = int((time.monotonic() - started) * 1000)
        marker.save(update_fields=["processing_time_ms"])
    return HandleOutcome.PROCESSED
