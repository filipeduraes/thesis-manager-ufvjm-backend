"""
Tests of the asynchronous defense adjudication consumer.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import ProfileRole
from adjudication.consumer import CONSUMER_GROUP, HandleOutcome, handle_event
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from theses.defense_committee import add_institutional_member
from theses.defense_evaluation import record_committee_evaluation
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseResult, DefenseStatus, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee
from theses.tests.test_api import create_user

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class AdjudicationConsumerTests(TestCase):
    """The result is calculated only when all required evaluations are in."""

    def setUp(self) -> None:
        cache.clear()
        department = Department.objects.create(name="Computação")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.course = Course.objects.create(department=department, coordinator=self.admin, name="CC", code="CC001")
        advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        evaluator1 = create_user("ev1@ufvjm.edu.br", "Avaliador Um", ProfileRole.ADVISOR)
        evaluator2 = create_user("ev2@ufvjm.edu.br", "Avaliador Dois", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.member1 = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, evaluator1, actor=self.admin)
        self.member2 = add_institutional_member(self.defense, CommitteeRole.MEMBER, evaluator2, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        self.evaluator1 = evaluator1
        self.evaluator2 = evaluator2

    def evaluate(self, member, actor, decision: str) -> EventEnvelope:
        """Record one evaluation and return the queued event."""
        with transaction.atomic():
            record_committee_evaluation(Defense.objects.get(pk=self.defense.pk), member, decision=decision, score="8.00", notes="Parecer de teste.", actor=actor)
        event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EVALUATION_RECORDED).order_by("created_at").last()
        return EventEnvelope.from_outbox(event)

    def status(self) -> str:
        """Return the persisted defense status."""
        return Defense.objects.get(pk=self.defense.pk).status

    def test_waits_for_all_evaluations(self) -> None:
        self.assertEqual(handle_event(self.evaluate(self.member1, self.evaluator1, DefenseResult.APPROVED)), HandleOutcome.PROCESSED)
        self.assertEqual(self.status(), DefenseStatus.IN_PROGRESS)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            self.assertEqual(handle_event(self.evaluate(self.member2, self.evaluator2, DefenseResult.APPROVED)), HandleOutcome.PROCESSED)
        self.assertEqual(self.status(), DefenseStatus.COMPLETED)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED)

    def test_duplicate_event_is_ignored(self) -> None:
        envelope = self.evaluate(self.member1, self.evaluator1, DefenseResult.REJECTED)
        self.assertEqual(handle_event(envelope), HandleOutcome.PROCESSED)
        self.assertEqual(handle_event(envelope), HandleOutcome.DUPLICATE)
        self.assertEqual(ProcessedEvent.objects.filter(consumer_group=CONSUMER_GROUP).count(), 1)

    def test_event_of_another_consumer_is_skipped(self) -> None:
        envelope = self.evaluate(self.member1, self.evaluator1, DefenseResult.REJECTED)
        other = EventEnvelope(**{**envelope.__dict__, "event_type": "thesis.submitted", "payload": {}})
        self.assertEqual(handle_event(other), HandleOutcome.SKIPPED)
