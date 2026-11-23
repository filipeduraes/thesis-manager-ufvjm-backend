"""
Tests for the outbox events recorded by thesis and defense transitions.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User
from adjudication.consumer import handle_event as handle_adjudication_event
from events.catalog import PAYLOAD_KEYS, EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, OutboxStatus, ProcessedEvent
from theses.defense_committee import add_institutional_member
from theses.defense_evaluation import record_committee_evaluation
from theses.defense_scheduling import close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseModality, DefenseResult, DocumentType, ReviewDecision, Thesis, ThesisStatus
from theses.services import upload_thesis_document, validate_thesis_document
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close
from theses.tests.test_api import TEST_CACHES, create_user, pdf_upload


def events_of(event_type: str) -> list[OutboxEvent]:
    return list(OutboxEvent.objects.filter(event_type=event_type).order_by("created_at"))


def assert_payload_has_no_personal_data(test: TestCase, users: list[User], extra_terms: list[str]) -> None:
    serialized = json.dumps([event.payload for event in OutboxEvent.objects.all()], ensure_ascii=False)
    for user in users:
        test.assertNotIn(user.email, serialized)
        test.assertNotIn(user.name, serialized)
    for term in extra_terms:
        test.assertNotIn(term, serialized)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ThesisOutboxEventTests(TestCase):
    """Submission, manual defense result and publication record their events in the same transaction."""

    def setUp(self) -> None:
        self.media_root = tempfile.mkdtemp(prefix="thesis-media-")
        self.override_media = override_settings(MEDIA_ROOT=self.media_root)
        self.override_media.enable()
        self.addCleanup(self.override_media.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        self.department = Department.objects.create(name="Departamento de Computação")
        self.course = Course.objects.create(department=self.department, name="Sistemas de Informação", code="SI")
        self.student = create_user("aluno@ufvjm.edu.br", "Aluno Teste", ProfileRole.STUDENT)
        self.advisor = create_user("orientador@ufvjm.edu.br", "Orientador Teste", ProfileRole.ADVISOR)
        self.coordinator = create_user("coordenador@ufvjm.edu.br", "Coordenador Teste", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.coordinator_email = self.coordinator.email
        self.course.save(update_fields=["coordinator", "coordinator_email"])
        self.client = APIClient()

    def _save_draft(self) -> None:
        self.client.force_authenticate(user=self.student)
        payload = {"title": "Monografia de teste", "abstract": "Resumo.", "courseId": str(self.course.pk), "advisorId": str(self.advisor.pk), "keywords": ["Teste"]}
        self.client.put("/api/v1/theses/me", payload, format="json")

    def _submit(self) -> Thesis:
        self._save_draft()
        self.client.post("/api/v1/theses/me/files", {"file": pdf_upload()}, format="multipart")
        response = self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.assertEqual(response.status_code, 200)
        return Thesis.objects.get(pk=response.data["thesis"]["id"])

    def _approve_by_advisor(self, thesis: Thesis) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.post(f"/api/v1/theses/{thesis.pk}/reviews", {"decision": ReviewDecision.APPROVED, "comments": "Aprovada."}, format="json")
        self.assertEqual(response.status_code, 200)

    def _record_result(self, thesis: Thesis, result: str) -> int:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.post(f"/api/v1/theses/{thesis.pk}/defense-result", {"result": result, "reason": "Justificativa sigilosa."}, format="json")
        return response.status_code

    def test_submission_records_thesis_submitted(self) -> None:
        thesis = self._submit()
        [event] = events_of(EventType.THESIS_SUBMITTED)
        self.assertEqual(event.status, OutboxStatus.PENDING)
        self.assertEqual((event.aggregate_type, event.aggregate_id), ("Thesis", thesis.pk))
        self.assertEqual(set(event.payload), PAYLOAD_KEYS[EventType.THESIS_SUBMITTED])
        self.assertEqual(event.payload["thesisId"], str(thesis.pk))
        self.assertEqual(event.payload["versionNumber"], 1)
        self.assertEqual(event.payload["submittedAt"], thesis.submitted_at.isoformat())

    def test_failed_submission_records_no_event(self) -> None:
        self._save_draft()
        response = self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_manual_defense_result_records_thesis_defense_result_recorded(self) -> None:
        thesis = self._submit()
        self._approve_by_advisor(thesis)
        self.assertEqual(self._record_result(thesis, DefenseResult.APPROVED_WITH_REMARKS), 200)
        [event] = events_of(EventType.THESIS_DEFENSE_RESULT_RECORDED)
        self.assertEqual((event.aggregate_type, event.aggregate_id), ("Thesis", thesis.pk))
        self.assertEqual(event.payload, {"thesisId": str(thesis.pk), "result": DefenseResult.APPROVED_WITH_REMARKS, "thesisNewStatus": ThesisStatus.APPROVED_WITH_REMARKS})
        self.assertFalse(OutboxEvent.objects.filter(event_type=EventType.DEFENSE_COMPLETED).exists())
        assert_payload_has_no_personal_data(self, [self.student, self.advisor, self.coordinator], ["Justificativa sigilosa."])

    def test_manual_defense_result_rejected_by_state_records_no_event(self) -> None:
        thesis = self._submit()
        self.assertEqual(self._record_result(thesis, DefenseResult.APPROVED), 400)
        self.assertFalse(OutboxEvent.objects.filter(event_type=EventType.THESIS_DEFENSE_RESULT_RECORDED).exists())

    def test_publication_records_thesis_published(self) -> None:
        thesis = self._submit()
        self._approve_by_advisor(thesis)
        self._record_result(thesis, DefenseResult.APPROVED)
        thesis.refresh_from_db()
        authorization = upload_thesis_document(self.student, thesis, DocumentType.PUBLICATION_AUTHORIZATION, pdf_upload("autorizacao.pdf"))
        validate_thesis_document(self.coordinator, thesis, authorization)
        response = self.client.post(f"/api/v1/theses/{thesis.pk}/publish", {}, format="json")
        self.assertEqual(response.status_code, 200)
        thesis.refresh_from_db()
        [event] = events_of(EventType.THESIS_PUBLISHED)
        self.assertEqual(event.payload, {"thesisId": str(thesis.pk), "referenceCode": thesis.reference_code, "publishedAt": thesis.published_at.isoformat()})

    def test_publication_without_authorization_records_no_event(self) -> None:
        thesis = self._submit()
        self._approve_by_advisor(thesis)
        self._record_result(thesis, DefenseResult.APPROVED)
        response = self.client.post(f"/api/v1/theses/{thesis.pk}/publish", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertFalse(OutboxEvent.objects.filter(event_type=EventType.THESIS_PUBLISHED).exists())


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseOutboxEventTests(TestCase):
    """Scheduling, evaluations and the committee close record their events atomically."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.evaluator1 = create_user("ev1@ufvjm.edu.br", "Avaliador Um", ProfileRole.ADVISOR)
        self.evaluator2 = create_user("ev2@ufvjm.edu.br", "Avaliador Dois", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.meeting_link = "https://meet.example.com/sala-sigilosa"

    def _schedule_and_start(self) -> Defense:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location=self.meeting_link, actor=self.admin)
        self.member1 = add_institutional_member(defense, CommitteeRole.PRESIDENT, self.evaluator1, actor=self.admin)
        self.member2 = add_institutional_member(defense, CommitteeRole.MEMBER, self.evaluator2, actor=self.admin)
        confirm_committee(defense)
        start_defense(defense, actor=self.admin)
        return Defense.objects.get(pk=defense.pk)

    def _drain_adjudication(self) -> None:
        """Process queued evaluation events like the adjudication worker."""
        processed = set(ProcessedEvent.objects.filter(consumer_group="adjudication").values_list("event_id", flat=True))
        events = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EVALUATION_RECORDED).exclude(pk__in=processed).order_by("created_at")
        for event in events:
            handle_adjudication_event(EventEnvelope.from_outbox(event))

    def _evaluate(self, member, actor: User, decision: str) -> bool:
        defense = Defense.objects.get(pk=member.defense_id)
        outcome = record_committee_evaluation(defense, member, decision=decision, score="8.00", notes="Parecer sigiloso do avaliador.", actor=actor)
        self._drain_adjudication()
        return outcome.result_calculation_queued

    def test_scheduling_records_defense_scheduled_without_location(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location=self.meeting_link, actor=self.admin)
        [event] = events_of(EventType.DEFENSE_SCHEDULED)
        self.assertEqual((event.aggregate_type, event.aggregate_id), ("Defense", defense.pk))
        self.assertEqual(
            event.payload,
            {
                "defenseId": str(defense.pk),
                "thesisId": str(self.thesis.pk),
                "scheduledDatetime": defense.scheduled_datetime.isoformat(),
                "durationMinutes": 120,
                "modality": DefenseModality.REMOTE,
            },
        )
        assert_payload_has_no_personal_data(self, [self.admin, self.advisor], [self.meeting_link])

    def test_failed_scheduling_records_no_event(self) -> None:
        self.thesis.status = ThesisStatus.SUBMITTED
        self.thesis.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_evaluations_record_events_without_decision_and_flag_updates(self) -> None:
        defense = self._schedule_and_start()
        self._evaluate(self.member1, self.evaluator1, DefenseResult.APPROVED)
        self._evaluate(self.member1, self.evaluator1, DefenseResult.APPROVED_WITH_REMARKS)
        first, second = events_of(EventType.DEFENSE_EVALUATION_RECORDED)
        self.assertEqual(first.payload, {"defenseId": str(defense.pk), "thesisId": str(self.thesis.pk), "memberId": str(self.member1.pk), "isUpdate": False})
        self.assertTrue(second.payload["isUpdate"])
        self.assertFalse(events_of(EventType.DEFENSE_COMPLETED))
        assert_payload_has_no_personal_data(self, [self.evaluator1], ["Parecer sigiloso do avaliador.", DefenseResult.APPROVED_WITH_REMARKS])

    def test_automatic_close_records_defense_completed(self) -> None:
        defense = self._schedule_and_start()
        self._evaluate(self.member1, self.evaluator1, DefenseResult.APPROVED)
        calculation_queued = self._evaluate(self.member2, self.evaluator2, DefenseResult.REJECTED)
        self.assertTrue(calculation_queued)
        self.assertEqual(len(events_of(EventType.DEFENSE_EVALUATION_RECORDED)), 2)
        [event] = events_of(EventType.DEFENSE_COMPLETED)
        self.assertEqual(
            event.payload,
            {"defenseId": str(defense.pk), "thesisId": str(self.thesis.pk), "result": DefenseResult.REJECTED, "thesisNewStatus": ThesisStatus.REJECTED, "automatic": True},
        )
        evaluation_event = events_of(EventType.DEFENSE_EVALUATION_RECORDED)[-1]
        self.assertLess(evaluation_event.created_at, event.created_at)

    def test_failed_automatic_close_keeps_evaluation_event_only(self) -> None:
        self._schedule_and_start()
        self._evaluate(self.member1, self.evaluator1, DefenseResult.APPROVED)
        with self.assertLogs("adjudication.consumer", level="WARNING"):
            calculation_queued = self._evaluate(self.member2, self.evaluator2, DefenseResult.APPROVED)
        self.assertTrue(calculation_queued)
        self.assertEqual(len(events_of(EventType.DEFENSE_EVALUATION_RECORDED)), 2)
        self.assertFalse(events_of(EventType.DEFENSE_COMPLETED))

    def test_manual_close_records_defense_completed_not_automatic(self) -> None:
        defense = self._schedule_and_start()
        record_evaluation_without_auto_close(self.member1, DefenseResult.APPROVED)
        record_evaluation_without_auto_close(self.member2, DefenseResult.APPROVED_WITH_REMARKS)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(defense, actor=self.admin)
        [event] = events_of(EventType.DEFENSE_COMPLETED)
        self.assertEqual(event.payload["result"], DefenseResult.APPROVED_WITH_REMARKS)
        self.assertEqual(event.payload["thesisNewStatus"], ThesisStatus.APPROVED_WITH_REMARKS)
        self.assertFalse(event.payload["automatic"])
