"""
Tests for the automatic publication after approval, correction and authorization.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import AuditAction, AuditLog, ProfileRole
from events.catalog import EventType
from events.models import OutboxEvent
from theses.defense_committee import add_institutional_member
from theses.defense_evaluation import record_committee_evaluation
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseResult, DocumentType, Thesis, ThesisStatus
from theses.repository_cache import VERSION_KEY
from theses.services import PublicationTrigger, publication_requirements, publish_thesis, record_defense_result, upload_thesis_document, upload_thesis_file, validate_thesis_document
from theses.tests.defense_helpers import confirm_committee
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user, pdf_upload


class AutoPublicationTestCase(RepositoryTestCase):
    """Common fixtures: a coordinator and an advisor-approved thesis."""

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenação SI", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])
        self.thesis = create_thesis(ThesisSpec(title="Publicação automática", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Fluxo"], status=ThesisStatus.ADVISOR_APPROVED))

    def refresh(self) -> Thesis:
        self.thesis.refresh_from_db()
        return self.thesis

    def submit_authorization(self) -> None:
        self.authorization = upload_thesis_document(self.student, self.thesis, DocumentType.PUBLICATION_AUTHORIZATION, pdf_upload("termo-assinado.pdf"))

    def validate_authorization(self) -> None:
        validate_thesis_document(self.coordinator, self.thesis, self.authorization)

    def upload_correction(self) -> None:
        upload_thesis_file(self.student, self.thesis, pdf_upload("versao-corrigida.pdf"), "Correções pedidas pela banca.")

    def assert_published_automatically(self, trigger: str) -> None:
        thesis = self.refresh()
        self.assertEqual(thesis.status, ThesisStatus.PUBLISHED)
        self.assertTrue(thesis.is_public)
        self.assertIsNotNone(thesis.published_at)
        self.assertIsNotNone(thesis.defense_year)
        entry = thesis.status_history.get(new_status=ThesisStatus.PUBLISHED)
        self.assertIsNone(entry.changed_by)
        self.assertEqual(entry.metadata["trigger"], trigger)
        self.assertTrue(entry.metadata["automatic"])
        audit = AuditLog.objects.filter(action=AuditAction.PUBLISH, entity_id=str(thesis.pk)).get()
        self.assertEqual(audit.metadata["trigger"], trigger)
        self.assertEqual(OutboxEvent.objects.filter(event_type=EventType.THESIS_PUBLISHED, aggregate_id=thesis.pk).count(), 1)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}").status_code, 200)

    def assert_not_published(self) -> None:
        thesis = self.refresh()
        self.assertNotEqual(thesis.status, ThesisStatus.PUBLISHED)
        self.assertFalse(thesis.is_public)
        self.assertFalse(OutboxEvent.objects.filter(event_type=EventType.THESIS_PUBLISHED).exists())
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}").status_code, 404)


class ApprovedWithoutRemarksTests(AutoPublicationTestCase):
    """Approval without remarks needs only the validated authorization."""

    def test_authorization_validated_after_result_publishes(self) -> None:
        record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED)
        self.assert_not_published()
        self.submit_authorization()
        self.assert_not_published()
        self.validate_authorization()
        self.assert_published_automatically(PublicationTrigger.AUTHORIZATION_VALIDATED)

    def test_result_recorded_after_authorization_publishes(self) -> None:
        self.submit_authorization()
        self.validate_authorization()
        self.assert_not_published()
        thesis = record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED)
        self.assertEqual(thesis.status, ThesisStatus.PUBLISHED)
        self.assert_published_automatically(PublicationTrigger.DEFENSE_RESULT)

    def test_rejection_never_publishes(self) -> None:
        self.submit_authorization()
        self.validate_authorization()
        record_defense_result(self.coordinator, self.thesis, DefenseResult.REJECTED)
        self.assert_not_published()
        self.assertEqual(self.refresh().status, ThesisStatus.REJECTED)

    def test_publication_invalidates_the_search_cache(self) -> None:
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 0)
        version = cache.get(VERSION_KEY)
        record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED)
        self.submit_authorization()
        self.validate_authorization()
        self.assertNotEqual(cache.get(VERSION_KEY), version)
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 1)

    def test_tampered_pdf_blocks_publication(self) -> None:
        record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED)
        current = self.thesis.files.get(is_current=True)
        with open(f"{self.media_root}/{current.stored_file.storage_path}", "ab") as handle:
            handle.write(b"adulterado")
        self.submit_authorization()
        with self.assertLogs("theses.services", level="WARNING"):
            self.validate_authorization()
        self.assert_not_published()
        with self.assertRaisesMessage(ValidationError, "integridade"):
            publish_thesis(self.coordinator, self.thesis)

    def test_manual_publish_is_idempotent_after_automatic_publication(self) -> None:
        record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED)
        self.submit_authorization()
        self.validate_authorization()
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/publish", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["thesis"]["status"], ThesisStatus.PUBLISHED)
        self.assertEqual(self.thesis.status_history.filter(new_status=ThesisStatus.PUBLISHED).count(), 1)
        self.assertEqual(OutboxEvent.objects.filter(event_type=EventType.THESIS_PUBLISHED).count(), 1)


class ApprovedWithRemarksTests(AutoPublicationTestCase):
    """Approval with remarks also needs the corrected version."""

    def setUp(self) -> None:
        super().setUp()
        record_defense_result(self.coordinator, self.thesis, DefenseResult.APPROVED_WITH_REMARKS)

    def test_requires_correction_and_authorization_in_any_order(self) -> None:
        self.submit_authorization()
        self.validate_authorization()
        self.assert_not_published()
        requirements = publication_requirements(self.refresh())
        self.assertTrue(requirements.correction_required)
        self.assertFalse(requirements.correction_submitted)
        self.upload_correction()
        self.assert_published_automatically(PublicationTrigger.CORRECTED_VERSION)
        current = self.thesis.files.get(is_current=True)
        self.assertTrue(current.is_correction)
        self.assertTrue(self.thesis.status_history.filter(metadata__event="CORRECTED_VERSION_UPLOADED").exists())

    def test_correction_first_then_authorization(self) -> None:
        self.upload_correction()
        self.assert_not_published()
        self.submit_authorization()
        self.validate_authorization()
        self.assert_published_automatically(PublicationTrigger.AUTHORIZATION_VALIDATED)

    def test_manual_publish_without_correction_is_refused(self) -> None:
        self.submit_authorization()
        self.validate_authorization()
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/publish", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("versão corrigida", response.data["detail"])

    def test_student_payload_flags_the_correction_upload(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.get("/api/v1/theses/me")
        thesis_payload = response.data["thesis"]
        self.assertTrue(thesis_payload["permissions"]["canUploadCorrection"])
        self.assertTrue(thesis_payload["permissions"]["canUpload"])
        self.assertEqual(thesis_payload["publicationRequirements"]["correctionRequired"], True)
        upload = self.client.post("/api/v1/theses/me/files", {"file": pdf_upload("corrigida-api.pdf")}, format="multipart")
        self.assertEqual(upload.status_code, 201)
        self.assertTrue(upload.data["file"]["isCorrection"])
        self.assertTrue(upload.data["thesis"]["publicationRequirements"]["correctionSubmitted"])

    def test_only_the_main_author_uploads_the_correction(self) -> None:
        with self.assertRaises(ValidationError):
            upload_thesis_file(self.other_student, self.thesis, pdf_upload("intruso.pdf"))

    def test_approved_without_remarks_does_not_accept_new_versions(self) -> None:
        other = create_thesis(ThesisSpec(title="Sem ressalvas", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.APPROVED))
        with self.assertRaisesMessage(ValidationError, "não pode mais ser editada"):
            upload_thesis_file(self.other_student, other, pdf_upload("nova.pdf"))


class DefenseCloseTriggersPublicationTests(AutoPublicationTestCase):
    """The committee close publishes the thesis when the authorization is already validated."""

    def test_last_evaluation_closes_and_publishes(self) -> None:
        admin = create_user("admin@ufvjm.edu.br", "Administração", ProfileRole.ADMIN)
        evaluator = create_user("avaliador@ufvjm.edu.br", "Avaliador", ProfileRole.ADVISOR)
        self.submit_authorization()
        self.validate_authorization()
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=3), location="Sala 1", actor=admin)
        president = add_institutional_member(defense, CommitteeRole.PRESIDENT, self.other_advisor, actor=admin)
        member = add_institutional_member(defense, CommitteeRole.MEMBER, evaluator, actor=admin)
        confirm_committee(defense)
        start_defense(defense, actor=admin)
        record_committee_evaluation(Defense.objects.get(pk=defense.pk), president, decision=DefenseResult.APPROVED, actor=self.other_advisor)
        self.assert_not_published()
        outcome = record_committee_evaluation(Defense.objects.get(pk=defense.pk), member, decision=DefenseResult.APPROVED, actor=evaluator)
        self.assertIsNone(outcome.auto_close_error)
        self.assert_published_automatically(PublicationTrigger.DEFENSE_CLOSED)
        defense.refresh_from_db()
        expected_date = timezone.localtime(defense.scheduled_datetime, timezone=timezone.get_fixed_timezone(-180)).date()
        self.assertEqual(self.refresh().defense_date, expected_date)
