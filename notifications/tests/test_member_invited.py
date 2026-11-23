"""
Tests for the committee member invitation notification.
"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import ProfileRole
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from notifications.consumer import CONSUMER_GROUP, HandleOutcome, handle_event
from notifications.tests.test_consumer import create_user
from theses.defense_committee import add_external_member, add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, DefenseMember, Thesis, ThesisAuthor, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", FRONTEND_URL="https://tcc.example.com")
class MemberInvitationTests(TestCase):
    """Adding a committee member notifies that member without leaking tokens."""

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.course = Course.objects.create(department=department, coordinator=self.admin, name="CC", code="CC001")
        student = create_user("aluno@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        advisor = create_user("orientador@ufvjm.edu.br", "Orientadora", ProfileRole.ADVISOR)
        self.evaluator = create_user("avaliador@ufvjm.edu.br", "Avaliador", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=advisor.pk, title="Banca distribuída", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author_id=student.pk, position=1, is_main_author=True)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)

    def handle_last(self) -> HandleOutcome:
        """Handle the most recent member-invited event."""
        event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_MEMBER_INVITED).latest("created_at")
        return handle_event(EventEnvelope.from_outbox(event))

    def test_institutional_member_receives_invitation(self) -> None:
        with transaction.atomic():
            add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.evaluator, actor=self.admin)
        self.assertEqual(self.handle_last(), HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["avaliador@ufvjm.edu.br"])
        self.assertIn("Banca distribuída", mail.outbox[0].subject)
        self.assertEqual(ProcessedEvent.objects.filter(consumer_group=CONSUMER_GROUP).count(), 1)

    def test_external_member_receives_token_only_by_email(self) -> None:
        with transaction.atomic():
            add_external_member(self.defense, CommitteeRole.MEMBER, name="Dr. Externo", email="externo@example.com", institution="UFMG", actor=self.admin)
        event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED).latest("created_at")
        self.assertEqual(handle_event(EventEnvelope.from_outbox(event)), HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["externo@example.com"])
        self.assertIn("UFVJM-", mail.outbox[0].body)
        self.assertTrue(DefenseMember.objects.filter(defense=self.defense, email="externo@example.com").exists())
