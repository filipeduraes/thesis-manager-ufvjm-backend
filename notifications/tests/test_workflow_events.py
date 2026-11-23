"""Tests of cancellation, rescheduling and advisor review notifications."""

from __future__ import annotations

from datetime import date, timedelta

from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent
from notifications.consumer import HandleOutcome, handle_event
from theses.events import record_defense_cancelled, record_defense_rescheduled, record_thesis_review_recorded
from theses.defense_scheduling import schedule_defense
from theses.models import ReviewDecision, ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, FRONTEND_URL="https://tcc.example.com")
class WorkflowEventNotificationTests(RepositoryTestCase):
    """The new workflow events produce the expected e-mails."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.thesis = create_thesis(ThesisSpec(title="Tese eventos", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["teste"], defense_date=date(2026, 6, 10), status=ThesisStatus.ADVISOR_APPROVED))
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", "ADMIN")
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)

    def _handle(self, event: OutboxEvent) -> HandleOutcome:
        return handle_event(EventEnvelope.from_outbox(event))

    def test_cancellation_notifies_author_and_advisor(self) -> None:
        with transaction.atomic():
            event = record_defense_cancelled(self.defense, previous_status="SCHEDULED")
        self.assertEqual(self._handle(event), HandleOutcome.PROCESSED)
        recipients = sorted(address for message in mail.outbox for address in message.to)
        self.assertEqual(recipients, sorted([self.student.email, self.advisor.email]))

    def test_reschedule_notifies_author_and_advisor(self) -> None:
        with transaction.atomic():
            event = record_defense_rescheduled(self.defense)
        self.assertEqual(self._handle(event), HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 2)

    def test_review_notifies_author_with_decision_label(self) -> None:
        with transaction.atomic():
            event = record_thesis_review_recorded(self.thesis, ReviewDecision.APPROVED)
        self.assertEqual(self._handle(event), HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(ReviewDecision.APPROVED.label, mail.outbox[0].body)
