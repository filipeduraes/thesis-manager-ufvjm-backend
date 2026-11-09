"""
Tests for the thesis submission, review and publication API.
"""

from __future__ import annotations

import io
import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from pypdf import PdfWriter
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.models import (
    DocumentStatus,
    DocumentType,
    ReviewDecision,
    StoredFile,
    Thesis,
    ThesisDocument,
    ThesisStatus,
)
from theses.services import record_defense_result, upload_thesis_document, validate_thesis_document

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "theses-test-cache",
    }
}


def build_pdf_bytes(text: str = "Monografia de teste") -> bytes:
    """
    Build a minimal valid PDF for upload tests.

    Args:
        text: Marker written to the PDF metadata so different versions have different bytes.

    Returns:
        bytes: PDF content.
    """
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.add_metadata({"/Title": text})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_upload(name: str = "monografia.pdf") -> SimpleUploadedFile:
    """
    Build a multipart PDF upload.

    Args:
        name: Original filename.

    Returns:
        SimpleUploadedFile: Upload object.
    """
    return SimpleUploadedFile(name, build_pdf_bytes(name), content_type="application/pdf")


def create_user(email: str, name: str, role: str) -> User:
    """
    Create an active user with a single role.

    Args:
        email: Institutional e-mail.
        name: Display name.
        role: Profile role code.

    Returns:
        User: Created user.
    """
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ThesisApiTests(TestCase):
    """
    Exercise the complete student and advisor thesis workflow.
    """

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

    def authenticate(self, user: User) -> None:
        """
        Authenticate the test client as a user.

        Args:
            user: User to authenticate.
        """
        self.client.force_authenticate(user=user)

    def test_student_can_read_empty_context(self) -> None:
        self.authenticate(self.student)
        response = self.client.get("/api/v1/theses/me")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["thesis"])
        self.assertEqual(response.data["courses"][0]["id"], str(self.course.pk))
        self.assertEqual(response.data["advisors"][0]["id"], str(self.advisor.pk))

    def test_student_creates_draft_uploads_and_submits(self) -> None:
        self.authenticate(self.student)
        response = self.client.put(
            "/api/v1/theses/me",
            {
                "title": "Arquiteturas sustentáveis",
                "abstract": "Resumo da monografia de teste.",
                "courseId": str(self.course.pk),
                "advisorId": str(self.advisor.pk),
                "keywords": ["Sustentabilidade", "Cidades"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        thesis_id = response.data["thesis"]["id"]
        self.assertEqual(response.data["thesis"]["status"], ThesisStatus.DRAFT)

        upload_response = self.client.post("/api/v1/theses/me/files", {"file": pdf_upload()}, format="multipart")
        self.assertEqual(upload_response.status_code, 201)
        self.assertEqual(upload_response.data["file"]["versionNumber"], 1)
        self.assertEqual(upload_response.data["file"]["storedFile"]["sha256"], upload_response.data["file"]["storedFile"]["sha256"].lower())

        submit_response = self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.assertEqual(submit_response.status_code, 200)
        self.assertEqual(submit_response.data["thesis"]["status"], ThesisStatus.SUBMITTED)
        self.assertEqual(submit_response.data["thesis"]["files"][0]["isCurrent"], True)
        self.assertEqual(len(submit_response.data["thesis"]["statusHistory"]), 2)
        self.assertEqual(str(submit_response.data["thesis"]["id"]), thesis_id)

    def test_upload_rejects_non_pdf_and_duplicate_current_version(self) -> None:
        self.authenticate(self.student)
        self.client.put(
            "/api/v1/theses/me",
            {
                "title": "Monografia",
                "abstract": "Resumo",
                "courseId": str(self.course.pk),
                "advisorId": str(self.advisor.pk),
                "keywords": ["Teste"],
            },
            format="json",
        )
        invalid = SimpleUploadedFile("arquivo.pdf", b"not a pdf", content_type="application/pdf")
        response = self.client.post("/api/v1/theses/me/files", {"file": invalid}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PDF", response.data["detail"])

        self.client.post("/api/v1/theses/me/files", {"file": pdf_upload()}, format="multipart")
        duplicate = self.client.post("/api/v1/theses/me/files", {"file": pdf_upload()}, format="multipart")
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("idêntico", duplicate.data["detail"])

    def test_advisor_reviews_and_student_resubmits(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.advisor)
        list_response = self.client.get("/api/v1/theses?scope=advised")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.data["count"], 1)
        self.assertEqual(list_response.data["results"][0]["id"], str(thesis.pk))
        self.assertTrue(list_response.data["results"][0]["pendingReview"])

        review_response = self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.CHANGES_REQUESTED, "comments": "Ajuste a fundamentação."},
            format="json",
        )
        self.assertEqual(review_response.status_code, 200)
        self.assertEqual(review_response.data["thesis"]["status"], ThesisStatus.CHANGES_REQUESTED)
        self.assertEqual(review_response.data["review"]["decision"], ReviewDecision.CHANGES_REQUESTED)

        self.authenticate(self.student)
        upload_response = self.client.post("/api/v1/theses/me/files", {"file": pdf_upload("v2.pdf")}, format="multipart")
        self.assertEqual(upload_response.status_code, 201)
        self.assertEqual(upload_response.data["file"]["versionNumber"], 2)
        submit_response = self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.assertEqual(submit_response.status_code, 200)

        self.authenticate(self.advisor)
        approve_response = self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.APPROVED, "comments": "Aprovada."},
            format="json",
        )
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve_response.data["thesis"]["status"], ThesisStatus.ADVISOR_APPROVED)
        self.assertEqual(len(approve_response.data["thesis"]["files"]), 2)

    def test_review_requires_feedback_for_changes(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.advisor)
        response = self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.CHANGES_REQUESTED},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_defense_approval_purges_old_versions(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.advisor)
        self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.CHANGES_REQUESTED, "comments": "Ajuste a introdução."},
            format="json",
        )
        self.authenticate(self.student)
        self.client.post("/api/v1/theses/me/files", {"file": pdf_upload("v2.pdf")}, format="multipart")
        self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.authenticate(self.advisor)
        self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.APPROVED, "comments": "Aprovada."},
            format="json",
        )
        thesis.refresh_from_db()
        self.assertEqual(thesis.files.count(), 2)

        self.authenticate(self.coordinator)
        response = self.client.post(
            f"/api/v1/theses/{thesis.pk}/defense-result",
            {"result": "APPROVED", "reason": "Banca aprovou."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        thesis.refresh_from_db()
        self.assertEqual(thesis.status, ThesisStatus.APPROVED)
        self.assertEqual(thesis.files.count(), 1)
        self.assertTrue(thesis.files.first().is_current)

    def test_published_thesis_is_public_and_cannot_be_deleted(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.advisor)
        self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.APPROVED, "comments": "Aprovada."},
            format="json",
        )
        self.authenticate(self.coordinator)
        self.client.post(
            f"/api/v1/theses/{thesis.pk}/defense-result",
            {"result": "APPROVED"},
            format="json",
        )
        thesis.refresh_from_db()
        authorization = upload_thesis_document(
            self.student,
            thesis,
            DocumentType.PUBLICATION_AUTHORIZATION,
            pdf_upload("autorizacao.pdf"),
        )
        validate_thesis_document(self.coordinator, thesis, authorization)
        response = self.client.post(f"/api/v1/theses/{thesis.pk}/publish", {}, format="json")
        self.assertEqual(response.status_code, 200)
        thesis.refresh_from_db()
        self.assertEqual(thesis.status, ThesisStatus.PUBLISHED)
        self.assertTrue(thesis.is_public)

        self.client.force_authenticate(user=None)
        public_response = self.client.get("/api/v1/public/theses")
        self.assertEqual(public_response.status_code, 200)
        self.assertEqual(public_response.data["count"], 1)
        detail_response = self.client.get(f"/api/v1/public/theses/{thesis.pk}")
        self.assertEqual(detail_response.status_code, 200)
        download_response = self.client.get(f"/api/v1/public/theses/{thesis.pk}/download")
        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response["Content-Type"], "application/pdf")

        with self.assertRaises(Exception):
            thesis.delete()

    def test_download_requires_permission(self) -> None:
        thesis = self._create_submitted_thesis()
        current = thesis.files.first()
        self.authenticate(self.student)
        response = self.client.get(f"/api/v1/theses/{thesis.pk}/files/{current.pk}/download?disposition=inline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

        outsider = create_user("outro@ufvjm.edu.br", "Outro Aluno", ProfileRole.STUDENT)
        self.authenticate(outsider)
        denied = self.client.get(f"/api/v1/theses/{thesis.pk}/files/{current.pk}/download")
        self.assertEqual(denied.status_code, 404)

    def test_student_can_withdraw_submission_before_advisor_review(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.student)
        response = self.client.post("/api/v1/theses/me/withdraw", {}, format="json")
        self.assertEqual(response.status_code, 200)
        thesis.refresh_from_db()
        self.assertEqual(thesis.status, ThesisStatus.DRAFT)
        self.assertIsNone(thesis.submitted_at)
        self.assertTrue(response.data["thesis"]["permissions"]["canSubmit"])
        self.assertFalse(response.data["thesis"]["permissions"]["canWithdrawSubmission"])

    def test_student_cannot_withdraw_after_advisor_review(self) -> None:
        thesis = self._create_submitted_thesis()
        self.authenticate(self.advisor)
        review = self.client.post(
            f"/api/v1/theses/{thesis.pk}/reviews",
            {"decision": ReviewDecision.CHANGES_REQUESTED, "comments": "Ajuste necessário."},
            format="json",
        )
        self.assertEqual(review.status_code, 200)
        self.authenticate(self.student)
        response = self.client.post("/api/v1/theses/me/withdraw", {}, format="json")
        self.assertEqual(response.status_code, 400)
        thesis.refresh_from_db()
        self.assertEqual(thesis.status, ThesisStatus.CHANGES_REQUESTED)

    def _create_submitted_thesis(self) -> Thesis:
        """
        Create a thesis with one file and submit it.

        Returns:
            Thesis: Submitted thesis.
        """
        self.authenticate(self.student)
        self.client.put(
            "/api/v1/theses/me",
            {
                "title": "Monografia de teste",
                "abstract": "Resumo da monografia de teste.",
                "courseId": str(self.course.pk),
                "advisorId": str(self.advisor.pk),
                "keywords": ["Teste", "Monografia"],
            },
            format="json",
        )
        self.client.post("/api/v1/theses/me/files", {"file": pdf_upload()}, format="multipart")
        response = self.client.post("/api/v1/theses/me/submit", {}, format="json")
        self.assertEqual(response.status_code, 200)
        return Thesis.objects.get(pk=response.data["thesis"]["id"])
