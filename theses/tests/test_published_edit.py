"""
Tests for the edition of published theses (metadata and PDF versions).
"""

from __future__ import annotations

from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import AuditAction, AuditLog, ProfileRole
from theses.models import ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import build_pdf_bytes, create_user, pdf_upload


class PublishedEditTestCase(RepositoryTestCase):
    """Fixtures: admin, course coordinators and a published thesis."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = create_user("admin@ufvjm.edu.br", "Administração", ProfileRole.ADMIN)
        self.coordinator = create_user("coord.si@ufvjm.edu.br", "Coordenação SI", ProfileRole.COORDINATOR)
        self.other_coordinator = create_user("coord.cc@ufvjm.edu.br", "Coordenação CC", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])
        self.other_course.coordinator = self.other_coordinator
        self.other_course.save(update_fields=["coordinator"])
        self.thesis = create_thesis(ThesisSpec(title="Título original", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Antiga"]))

    def patch(self, user, payload: dict[str, object]):
        self.client.force_authenticate(user=user)
        return self.client.patch(f"/api/v1/theses/{self.thesis.pk}/published", payload, format="json")

    def upload(self, user, upload: SimpleUploadedFile):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/api/v1/theses/{self.thesis.pk}/published/files", {"file": upload, "changeDescription": "Errata na página 12."}, format="multipart")


class PublishedMetadataEditTests(PublishedEditTestCase):
    """Metadata edits are recorded (who, what, when) and reach the public search."""

    def test_coordinator_edits_and_change_is_recorded(self) -> None:
        payload = {
            "title": "Título corrigido",
            "keywords": ["Nova", "Antiga"],
            "authors": [{"userId": str(self.student.pk)}, {"name": "Coautor Externo"}],
            "advisorId": str(self.other_advisor.pk),
            "defenseYear": 2022,
            "reason": "Correção solicitada pelo autor.",
        }
        with self.captureOnCommitCallbacks(execute=True):
            response = self.patch(self.coordinator, payload)
        self.assertEqual(response.status_code, 200, response.content)
        thesis_payload = response.data["thesis"]
        self.assertEqual(thesis_payload["title"], "Título corrigido")
        self.assertEqual(thesis_payload["advisor"]["id"], str(self.other_advisor.pk))
        self.assertEqual(sorted(thesis_payload["keywords"]), ["Antiga", "Nova"])
        self.assertEqual([author["name"] for author in thesis_payload["authors"]], [self.student.name, "Coautor Externo"])
        self.assertEqual(thesis_payload["status"], ThesisStatus.PUBLISHED)
        entry = self.thesis.status_history.get(metadata__event="PUBLISHED_METADATA_UPDATED")
        self.assertEqual(entry.changed_by, self.coordinator)
        self.assertEqual(entry.reason, "Correção solicitada pelo autor.")
        self.assertEqual(entry.metadata["changedFields"], ["advisorId", "authors", "defenseDate", "defenseYear", "keywords", "title"])
        self.assertIsNotNone(entry.changed_at)
        audit = AuditLog.objects.get(action=AuditAction.UPDATE, entity_id=str(self.thesis.pk), metadata__event="PUBLISHED_METADATA_UPDATED")
        self.assertEqual(audit.actor_user, self.coordinator)
        self.assertEqual(audit.changes["title"], {"from": "Título original", "to": "Título corrigido"})
        self.assertEqual(audit.changes["defenseYear"], {"from": 2024, "to": 2022})
        self.assertEqual(audit.changes["defenseDate"], {"from": "2024-06-10", "to": None})
        history_events = [item["metadata"].get("event") for item in thesis_payload["statusHistory"]]
        self.assertIn("PUBLISHED_METADATA_UPDATED", history_events)

    def test_edit_invalidates_the_public_cache(self) -> None:
        self.assertEqual(self.client.get("/api/v1/public/theses", {"q": "corrigido"}).data["count"], 0)
        self.assertEqual(self.client.get("/api/v1/public/theses", {"year": 2022}).data["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{self.thesis.pk}").data["thesis"]["title"], "Título original")
        with self.captureOnCommitCallbacks(execute=True):
            self.patch(self.admin, {"title": "Título corrigido", "defenseYear": 2022})
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/public/theses", {"q": "corrigido"}).data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/public/theses", {"year": 2022}).data["count"], 1)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{self.thesis.pk}").data["thesis"]["title"], "Título corrigido")
        self.assertEqual(self.client.get("/api/v1/public/theses/filters").data["years"], [{"year": 2022, "count": 1}])

    def test_permissions(self) -> None:
        self.assertEqual(self.patch(self.other_coordinator, {"title": "X"}).status_code, 404)
        self.assertEqual(self.patch(self.advisor, {"title": "X"}).status_code, 403)
        self.assertEqual(self.patch(self.student, {"title": "X"}).status_code, 403)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.title, "Título original")

    def test_moving_to_another_course_requires_managing_both(self) -> None:
        self.assertEqual(self.patch(self.coordinator, {"courseId": str(self.other_course.pk)}).status_code, 403)
        response = self.patch(self.admin, {"courseId": str(self.other_course.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["thesis"]["course"]["id"], str(self.other_course.pk))

    def test_invalid_or_empty_edits_are_refused(self) -> None:
        self.assertEqual(self.patch(self.admin, {}).status_code, 400)
        self.assertEqual(self.patch(self.admin, {"title": "Título original"}).status_code, 400)
        self.assertEqual(self.patch(self.admin, {"title": "   "}).status_code, 400)
        self.assertEqual(self.patch(self.admin, {"keywords": []}).status_code, 400)
        self.assertEqual(self.patch(self.admin, {"defenseYear": 3000}).status_code, 400)
        self.assertFalse(self.thesis.status_history.filter(metadata__event="PUBLISHED_METADATA_UPDATED").exists())

    def test_only_published_theses_are_edited_here(self) -> None:
        draft = create_thesis(ThesisSpec(title="Rascunho", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.DRAFT))
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/v1/theses/{draft.pk}/published", {"title": "Outro"}, format="json")
        self.assertEqual(response.status_code, 400)


class PublishedFileReplacementTests(PublishedEditTestCase):
    """A new PDF becomes a new version with its own hash; the previous one is kept."""

    def test_replacement_creates_a_new_version_and_keeps_the_previous(self) -> None:
        previous = self.thesis.files.get(is_current=True)
        previous_hash = previous.stored_file.sha256_hex
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload(self.coordinator, pdf_upload("errata.pdf"))
        self.assertEqual(response.status_code, 201, response.content)
        new_hash = response.data["file"]["storedFile"]["sha256"]
        self.assertNotEqual(new_hash, previous_hash)
        self.assertEqual(response.data["file"]["versionNumber"], 2)
        self.assertEqual(self.thesis.files.count(), 2)
        previous.refresh_from_db()
        self.assertFalse(previous.is_current)
        entry = self.thesis.status_history.get(metadata__event="PUBLISHED_FILE_REPLACED")
        self.assertEqual(entry.changed_by, self.coordinator)
        self.assertEqual(entry.metadata["previousSha256"], previous_hash)
        self.assertEqual(entry.metadata["sha256"], new_hash)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.UPLOAD, entity_id=str(self.thesis.pk), metadata__event="PUBLISHED_FILE_REPLACED").exists())
        self.client.force_authenticate(user=None)
        detail = self.client.get(f"/api/v1/public/theses/{self.thesis.pk}").data["thesis"]
        self.assertEqual(detail["integrity"]["sha256"], new_hash)
        self.assertEqual([version["sha256"] for version in detail["fileVersions"]], [new_hash, previous_hash])
        download = self.client.get(f"/api/v1/public/theses/{self.thesis.pk}/download")
        self.assertEqual(b"".join(download.streaming_content), build_pdf_bytes("errata.pdf"))

    def test_invalid_identical_or_foreign_pdf_is_refused(self) -> None:
        self.assertEqual(self.upload(self.admin, SimpleUploadedFile("x.pdf", b"texto")).status_code, 400)
        current_bytes = build_pdf_bytes(f"Título original-{self.thesis.pk}")
        self.assertEqual(self.upload(self.admin, SimpleUploadedFile("mesmo.pdf", current_bytes)).status_code, 400)
        other = create_thesis(ThesisSpec(title="Outra", course=self.course, advisor=self.advisor, authors=[self.other_student]))
        other_bytes = build_pdf_bytes(f"Outra-{other.pk}")
        foreign = self.upload(self.admin, SimpleUploadedFile("outra.pdf", other_bytes))
        self.assertEqual(foreign.status_code, 400)
        self.assertIn("outra monografia", foreign.data["detail"])
        self.assertEqual(self.thesis.files.count(), 1)

    def test_replacement_permissions(self) -> None:
        self.assertEqual(self.upload(self.other_coordinator, pdf_upload("a.pdf")).status_code, 404)
        self.assertEqual(self.upload(self.advisor, pdf_upload("b.pdf")).status_code, 403)
        self.assertEqual(self.upload(self.student, pdf_upload("c.pdf")).status_code, 403)
        self.assertEqual(self.thesis.files.count(), 1)
