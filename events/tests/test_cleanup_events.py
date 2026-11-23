"""Tests of the event retention command."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from uuid import uuid4

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from events.models import OutboxEvent, OutboxStatus, ProcessedEvent


class CleanupEventsTests(TestCase):
    """Only old, already published events and markers are removed."""

    def setUp(self) -> None:
        old = timezone.now() - timedelta(days=40)
        self.old_event = OutboxEvent.objects.create(
            aggregate_type="Thesis", aggregate_id=uuid4(), event_type="thesis.published",
            payload={}, status=OutboxStatus.COMPLETED, published_at=old,
        )
        OutboxEvent.objects.filter(pk=self.old_event.pk).update(published_at=old)
        self.recent_event = OutboxEvent.objects.create(
            aggregate_type="Thesis", aggregate_id=uuid4(), event_type="thesis.submitted",
            payload={}, status=OutboxStatus.PENDING,
        )
        self.old_marker = ProcessedEvent.objects.create(consumer_group="notifications", event_id=uuid4())
        ProcessedEvent.objects.filter(consumer_group=self.old_marker.consumer_group, event_id=self.old_marker.event_id).update(processed_at=old)
        self.recent_marker = ProcessedEvent.objects.create(consumer_group="indexing", event_id=uuid4())

    def test_dry_run_does_not_delete(self) -> None:
        call_command("cleanup_events", "--days", "30", "--dry-run", stdout=StringIO())
        self.assertEqual(OutboxEvent.objects.count(), 2)
        self.assertEqual(ProcessedEvent.objects.count(), 2)

    def test_removes_only_old_published_events_and_markers(self) -> None:
        call_command("cleanup_events", "--days", "30", stdout=StringIO())
        self.assertFalse(OutboxEvent.objects.filter(pk=self.old_event.pk).exists())
        self.assertTrue(OutboxEvent.objects.filter(pk=self.recent_event.pk).exists())
        self.assertFalse(ProcessedEvent.objects.filter(consumer_group="notifications").exists())
        self.assertTrue(ProcessedEvent.objects.filter(consumer_group="indexing").exists())
