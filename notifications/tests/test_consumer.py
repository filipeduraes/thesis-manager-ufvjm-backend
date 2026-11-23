"""
Tests for the idempotent notification handler (no broker involved).
"""

from __future__ import annotations

import smtplib
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation, issue_external_token
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from events.publisher import publish_pending_events
from events.tests.fakes import InMemoryEventBroker
from notifications.consumer import CONSUMER_GROUP, HandleOutcome, handle_event
from notifications.models import Notification, NotificationStatus, NotificationTemplate, NotificationTemplateCode
from notifications.rendering import TemplateContextError
from theses.defense_committee import add_institutional_member
from theses.defense_evaluation import record_committee_evaluation
from theses.defense_scheduling import schedule_defense, start_defense
from theses.events import record_defense_completed, record_thesis_defense_result_recorded, record_thesis_published, record_thesis_submitted
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseModality, DefenseResult, Thesis, ThesisAuthor, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def envelope_for(event: OutboxEvent) -> EventEnvelope:
    body = EventEnvelope.from_outbox(event).to_json_bytes()
    return EventEnvelope.from_json_bytes(body)


def latest_event(event_type: str) -> OutboxEvent:
    return OutboxEvent.objects.filter(event_type=event_type).latest("created_at")


def recipients_of(messages: list) -> list[str]:
    return sorted(address for message in messages for address in message.to)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", FRONTEND_URL="https://tcc.example.com")
class NotificationHandlerTests(TestCase):
    """Each event type reaches the agreed recipients exactly once."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenação", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.student = create_user("aluno@ufvjm.edu.br", "Ana Aluna", ProfileRole.STUDENT)
        self.advisor = create_user("orientador@ufvjm.edu.br", "Olga Orientadora", ProfileRole.ADVISOR)
        self.evaluator = create_user("avaliador@ufvjm.edu.br", "Vitor Avaliador", ProfileRole.ADVISOR)
        self.alternate_user = create_user("suplente@ufvjm.edu.br", "Sara Suplente", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Filas & <Eventos>", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author_id=self.student.pk, position=1, is_main_author=True)

    def _schedule_with_committee(self) -> Defense:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location="https://meet.example.com/sala", actor=self.admin)
        add_institutional_member(defense, CommitteeRole.PRESIDENT, self.advisor, actor=self.admin)
        add_institutional_member(defense, CommitteeRole.MEMBER, self.evaluator, actor=self.admin)
        add_institutional_member(defense, CommitteeRole.ALTERNATE, self.alternate_user, actor=self.admin)
        invitation = create_external_invitation(name="Edu Externo", email="externo@example.org", institution="UFMG", invited_by=self.admin)
        self.external_token = issue_external_token(invitation)
        DefenseMember.objects.create(defense=defense, role=CommitteeRole.MEMBER, external_invitation_id=invitation.pk, name="Edu Externo", email="externo@example.org")
        return defense

    def _handle_latest(self, event_type: str) -> HandleOutcome:
        mail.outbox.clear()
        return handle_event(envelope_for(latest_event(event_type)))

    def test_thesis_submitted_notifies_only_the_advisor(self) -> None:
        with transaction.atomic():
            record_thesis_submitted(self.thesis, 2)
        outcome = self._handle_latest(EventType.THESIS_SUBMITTED)
        self.assertEqual(outcome, HandleOutcome.PROCESSED)
        [message] = mail.outbox
        self.assertEqual(message.to, ["orientador@ufvjm.edu.br"])
        self.assertIn("Ana Aluna submeteu a versão 2", message.body)
        self.assertIn("Olá, Olga Orientadora.", message.body)
        self.assertIn("https://tcc.example.com", message.body)

    def test_defense_scheduled_notifies_authors_and_advisor_without_location(self) -> None:
        schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location="https://meet.example.com/sala", actor=self.admin)
        self._handle_latest(EventType.DEFENSE_SCHEDULED)
        self.assertEqual(recipients_of(mail.outbox), ["aluno@ufvjm.edu.br", "orientador@ufvjm.edu.br"])
        for message in mail.outbox:
            self.assertNotIn("meet.example.com", message.body)
            self.assertIn("modalidade remota", message.body)

    def test_evaluation_recorded_notifies_advisor_with_progress_only(self) -> None:
        defense = self._schedule_with_committee()
        confirm_committee(defense)
        start_defense(defense, actor=self.admin)
        member = DefenseMember.objects.get(defense=defense, institutional_member=self.evaluator)
        record_committee_evaluation(Defense.objects.get(pk=defense.pk), member, decision=DefenseResult.REJECTED, score="6.00", notes="Parecer sigiloso", actor=self.evaluator)
        self._handle_latest(EventType.DEFENSE_EVALUATION_RECORDED)
        [message] = mail.outbox
        self.assertEqual(message.to, ["orientador@ufvjm.edu.br"])
        self.assertIn("Avaliações registradas: 1 de 3", message.body)
        self.assertNotIn("Reprovada", message.body)
        self.assertNotIn("Parecer sigiloso", message.body)
        self.assertNotIn("Vitor", message.body)

    def test_defense_completed_notifies_authors_advisor_and_evaluating_members_once(self) -> None:
        defense = self._schedule_with_committee()
        with transaction.atomic():
            record_defense_completed(defense, DefenseResult.APPROVED_WITH_REMARKS, ThesisStatus.APPROVED_WITH_REMARKS, True)
        with self.assertLogs("notifications", level="INFO") as captured:
            self._handle_latest(EventType.DEFENSE_COMPLETED)
        expected = ["aluno@ufvjm.edu.br", "avaliador@ufvjm.edu.br", "externo@example.org", "orientador@ufvjm.edu.br"]
        self.assertEqual(recipients_of(mail.outbox), expected)
        for message in mail.outbox:
            self.assertEqual(len(message.to), 1)
            self.assertIn("Resultado: Aprovada com ressalvas.", message.body)
            self.assertNotIn(self.external_token, message.body)
            html_body, mimetype = message.alternatives[0]
            self.assertEqual(mimetype, "text/html")
            self.assertIn("Filas &amp; &lt;Eventos&gt;", html_body)
        external = Notification.objects.get(external_email="externo@example.org")
        self.assertIsNotNone(external.external_invitation_id)
        self.assertIsNone(external.user_id)
        log_output = "\n".join(captured.output)
        self.assertNotIn("@", log_output)

    def test_manual_result_notifies_authors_and_advisor(self) -> None:
        with transaction.atomic():
            record_thesis_defense_result_recorded(self.thesis, DefenseResult.REJECTED, ThesisStatus.REJECTED)
        self._handle_latest(EventType.THESIS_DEFENSE_RESULT_RECORDED)
        self.assertEqual(recipients_of(mail.outbox), ["aluno@ufvjm.edu.br", "orientador@ufvjm.edu.br"])
        self.assertIn("Resultado: Reprovada.", mail.outbox[0].body)

    def test_publication_notifies_authors_and_advisor(self) -> None:
        self.thesis.published_at = timezone.now()
        with transaction.atomic():
            record_thesis_published(self.thesis)
        self._handle_latest(EventType.THESIS_PUBLISHED)
        self.assertEqual(recipients_of(mail.outbox), ["aluno@ufvjm.edu.br", "orientador@ufvjm.edu.br"])
        self.assertIn(self.thesis.reference_code, mail.outbox[0].body)

    def test_coordinator_is_never_notified(self) -> None:
        with transaction.atomic():
            record_thesis_defense_result_recorded(self.thesis, DefenseResult.APPROVED, ThesisStatus.APPROVED)
            record_thesis_published(self.thesis)
        for event in OutboxEvent.objects.all():
            handle_event(envelope_for(event))
        self.assertFalse(Notification.objects.filter(user=self.coordinator).exists())

    def test_inactive_author_is_skipped(self) -> None:
        self.student.is_active = False
        self.student.save(update_fields=["is_active"])
        with transaction.atomic():
            record_thesis_published(self.thesis)
        self._handle_latest(EventType.THESIS_PUBLISHED)
        self.assertEqual(recipients_of(mail.outbox), ["orientador@ufvjm.edu.br"])


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class NotificationHandlerIdempotencyTests(TestCase):
    """Redeliveries, invalid events and failures never produce duplicate or partial state."""

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        course = Course.objects.create(department=department, name="CC", code="CC001")
        self.student = create_user("aluno@ufvjm.edu.br", "Ana Aluna", ProfileRole.STUDENT)
        self.advisor = create_user("orientador@ufvjm.edu.br", "Olga Orientadora", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author_id=self.student.pk, position=1, is_main_author=True)
        with transaction.atomic():
            self.event = record_thesis_published(self.thesis)
        self.envelope = envelope_for(self.event)

    def test_redelivery_is_duplicate_and_sends_nothing(self) -> None:
        self.assertEqual(handle_event(self.envelope), HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 2)
        with self.assertLogs("notifications.consumer", level="INFO"):
            self.assertEqual(handle_event(self.envelope), HandleOutcome.DUPLICATE)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(Notification.objects.count(), 2)
        marker = ProcessedEvent.objects.get(consumer_group=CONSUMER_GROUP, event_id=self.event.pk)
        self.assertIsNotNone(marker.processing_time_ms)

    def test_unknown_type_unsupported_version_and_bad_payload_are_rejected(self) -> None:
        variants = [
            EventEnvelope(**{**self.envelope.__dict__, "event_type": "thesis.unknown"}),
            EventEnvelope(**{**self.envelope.__dict__, "event_version": "2.0"}),
            EventEnvelope(**{**self.envelope.__dict__, "payload": {**self.envelope.payload, "email": "aluno@ufvjm.edu.br"}}),
        ]
        for variant in variants:
            with self.subTest(variant=variant.event_type), self.assertLogs("notifications.consumer", level="WARNING"):
                self.assertEqual(handle_event(variant), HandleOutcome.REJECTED)
        self.assertFalse(ProcessedEvent.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_delivery_failure_marks_notification_failed_and_keeps_marker(self) -> None:
        error = smtplib.SMTPRecipientsRefused({"aluno@ufvjm.edu.br": (550, b"mailbox unavailable aluno@ufvjm.edu.br")})
        with patch("notifications.delivery.EmailMultiAlternatives.send", side_effect=error), self.assertLogs("notifications", level="INFO") as captured:
            outcome = handle_event(self.envelope)
        self.assertEqual(outcome, HandleOutcome.PROCESSED)
        self.assertTrue(ProcessedEvent.objects.filter(event_id=self.event.pk).exists())
        for notification in Notification.objects.all():
            self.assertEqual(notification.status, NotificationStatus.FAILED)
            self.assertEqual(notification.attempts, 1)
            self.assertIsNotNone(notification.failed_at)
            self.assertIn("SMTPRecipientsRefused", notification.error_message)
            self.assertNotIn("aluno@ufvjm.edu.br", notification.error_message)
        self.assertNotIn("@", "\n".join(captured.output))

    def test_successful_delivery_marks_sent(self) -> None:
        handle_event(self.envelope)
        for notification in Notification.objects.all():
            self.assertEqual((notification.status, notification.attempts), (NotificationStatus.SENT, 1))
            self.assertIsNotNone(notification.sent_at)
            self.assertEqual(notification.event_id, self.event.pk)

    def test_rendering_error_rolls_back_marker_so_event_can_be_retried(self) -> None:
        with patch("notifications.consumer.render_notification", side_effect=TemplateContextError("missing")), self.assertRaises(TemplateContextError):
            handle_event(self.envelope)
        self.assertFalse(ProcessedEvent.objects.exists())
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(handle_event(self.envelope), HandleOutcome.PROCESSED)

    def test_missing_thesis_is_processed_without_notifications(self) -> None:
        payload = {**self.envelope.payload, "thesisId": str(uuid.uuid4())}
        orphan = EventEnvelope(**{**self.envelope.__dict__, "event_id": uuid.uuid4(), "payload": payload})
        with self.assertLogs("notifications.consumer", level="WARNING"):
            self.assertEqual(handle_event(orphan), HandleOutcome.PROCESSED)
        self.assertTrue(ProcessedEvent.objects.filter(event_id=orphan.event_id).exists())
        self.assertEqual(mail.outbox, [])

    def test_inactive_template_creates_no_notification(self) -> None:
        NotificationTemplate.objects.filter(code=NotificationTemplateCode.THESIS_PUBLISHED).update(is_active=False)
        with self.assertLogs("notifications.consumer", level="WARNING"):
            self.assertEqual(handle_event(self.envelope), HandleOutcome.PROCESSED)
        self.assertFalse(Notification.objects.exists())


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class EndToEndNotificationTests(TestCase):
    """Service transition → outbox → publisher (in-memory broker) → handler → e-mail."""

    def test_scheduling_a_defense_ends_in_e_mails(self) -> None:
        department = Department.objects.create(name="Computação")
        course = Course.objects.create(department=department, name="CC", code="CC001")
        admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        student = create_user("aluno@ufvjm.edu.br", "Ana Aluna", ProfileRole.STUDENT)
        advisor = create_user("orientador@ufvjm.edu.br", "Olga Orientadora", ProfileRole.ADVISOR)
        thesis = Thesis.objects.create(course_id=course.pk, advisor_id=advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=thesis, author_id=student.pk, position=1, is_main_author=True)
        schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=admin)
        self.assertEqual(mail.outbox, [])
        broker = InMemoryEventBroker()
        publish_pending_events(broker, batch_size=10)
        [published] = broker.published
        received = EventEnvelope.from_json_bytes(published.to_json_bytes())
        self.assertEqual(handle_event(received), HandleOutcome.PROCESSED)
        self.assertEqual(recipients_of(mail.outbox), ["aluno@ufvjm.edu.br", "orientador@ufvjm.edu.br"])
