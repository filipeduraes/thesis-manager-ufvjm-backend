"""
Tests for the manual registration of legacy theses in the public repository.
"""

from __future__ import annotations

import json

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from accounts.models import AuditAction, AuditLog, ProfileRole
from events.models import OutboxEvent
from theses.models import ProcessingStatus, Thesis, ThesisOrigin, ThesisStatus
from theses.tasks import extract_pending_thesis_text
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import build_pdf_bytes, create_user, pdf_upload


class LegacyImportTestCase(RepositoryTestCase):
    """Fixtures: admin, course coordinators and a valid legacy payload."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = create_user("admin@ufvjm.edu.br", "Administração", ProfileRole.ADMIN)
        self.coordinator = create_user("coord.si@ufvjm.edu.br", "Coordenação SI", ProfileRole.COORDINATOR)
        self.other_coordinator = create_user("coord.cc@ufvjm.edu.br", "Coordenação CC", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])
        self.other_course.coordinator = self.other_coordinator
        self.other_course.save(update_fields=["coordinator"])

    def payload(self, **overrides: object) -> dict[str, object]:
        data: dict[str, object] = {
            "title": "Arquitetura de redes no DECOM",
            "abstract": "Monografia defendida antes do sistema.",
            "keywords": ["Redes", "Acervo"],
            "authors": json.dumps([{"name": "Fulano de Tal (2015)"}, {"userId": str(self.student.pk)}]),
            "advisorId": str(self.advisor.pk),
            "courseId": str(self.course.pk),
            "defenseYear": 2015,
            "defenseDate": "2015-12-03",
            "result": "APPROVED",
            "file": pdf_upload("acervo-2015.pdf"),
        }
        data.update(overrides)
        return data

    def post(self, user, **overrides: object):
        self.client.force_authenticate(user=user)
        return self.client.post("/api/v1/theses/legacy", self.payload(**overrides), format="multipart")


class LegacyImportPermissionTests(LegacyImportTestCase):
    """Only administrators and coordinators of the course may register legacy theses."""

    def test_admin_imports_a_published_legacy_thesis(self) -> None:
        response = self.post(self.admin)
        self.assertEqual(response.status_code, 201, response.content)
        payload = response.data["thesis"]
        self.assertEqual(payload["status"], ThesisStatus.PUBLISHED)
        self.assertEqual(payload["origin"], ThesisOrigin.LEGACY_IMPORT)
        self.assertEqual(payload["defenseYear"], 2015)
        self.assertEqual([author["name"] for author in payload["authors"]], ["Fulano de Tal (2015)", self.student.name])
        self.assertIsNone(payload["authors"][0]["user"])
        self.assertEqual(payload["authors"][1]["user"]["id"], str(self.student.pk))
        thesis = Thesis.objects.get(pk=payload["id"])
        self.assertTrue(thesis.is_public)
        entry = thesis.status_history.get()
        self.assertEqual((entry.previous_status, entry.new_status, entry.changed_by), ("", ThesisStatus.PUBLISHED, self.admin))
        self.assertEqual(entry.metadata["event"], "LEGACY_IMPORT")
        self.assertEqual(entry.metadata["result"], "APPROVED")
        audit = AuditLog.objects.get(action=AuditAction.CREATE, entity_id=str(thesis.pk))
        self.assertEqual(audit.metadata["event"], "LEGACY_IMPORT")
        self.assertEqual(audit.metadata["sha256"], thesis.files.get().stored_file.sha256_hex)
        self.assertFalse(OutboxEvent.objects.exists())

    def test_coordinator_of_the_course_imports(self) -> None:
        self.assertEqual(self.post(self.coordinator).status_code, 201)

    def test_other_roles_are_refused(self) -> None:
        self.assertEqual(self.post(self.other_coordinator).status_code, 403)
        self.assertEqual(self.post(self.advisor).status_code, 403)
        self.assertEqual(self.post(self.student).status_code, 403)
        self.client.force_authenticate(user=None)
        anonymous = self.client.post("/api/v1/theses/legacy", self.payload(), format="multipart")
        self.assertIn(anonymous.status_code, {401, 403})
        self.assertFalse(Thesis.objects.exists())

    def test_coordinator_cannot_import_into_another_course(self) -> None:
        response = self.post(self.coordinator, courseId=str(self.other_course.pk))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Thesis.objects.exists())


class LegacyImportValidationTests(LegacyImportTestCase):
    """The PDF and the metadata go through the same validation as ordinary uploads."""

    def test_non_pdf_is_refused(self) -> None:
        fake = SimpleUploadedFile("acervo.pdf", b"isto nao e um pdf", content_type="application/pdf")
        response = self.post(self.admin, file=fake)
        self.assertEqual(response.status_code, 400)
        self.assertIn("PDF", response.data["detail"])
        self.assertFalse(Thesis.objects.exists())

    @override_settings(THESIS_MAX_PDF_SIZE_MB=1)
    def test_size_limit_is_enforced(self) -> None:
        large = SimpleUploadedFile("grande.pdf", b"%PDF-" + b"0" * (1024 * 1024 + 10), content_type="application/pdf")
        response = self.post(self.admin, file=large)
        self.assertEqual(response.status_code, 400)
        self.assertIn("limite", response.data["detail"])

    def test_same_pdf_cannot_be_registered_twice(self) -> None:
        content = build_pdf_bytes("mesmo-arquivo")
        self.assertEqual(self.post(self.admin, file=SimpleUploadedFile("a.pdf", content)).status_code, 201)
        duplicate = self.post(self.admin, title="Outra entrada", file=SimpleUploadedFile("b.pdf", content))
        self.assertEqual(duplicate.status_code, 400)
        self.assertIn("já está cadastrado", duplicate.data["detail"])
        self.assertEqual(Thesis.objects.count(), 1)

    def test_invalid_metadata_is_refused(self) -> None:
        next_year = timezone.now().year + 1
        cases = [
            {"result": "REJECTED"},
            {"defenseYear": next_year, "defenseDate": ""},
            {"defenseYear": "", "defenseDate": ""},
            {"defenseYear": 2016},
            {"authors": json.dumps([])},
            {"authors": "não é json"},
            {"keywords": []},
            {"advisorId": str(self.other_student.pk)},
            {"title": "   "},
        ]
        for overrides in cases:
            response = self.post(self.admin, file=pdf_upload("invalido.pdf"), **overrides)
            self.assertEqual(response.status_code, 400, overrides)
        self.assertFalse(Thesis.objects.exists())

    def test_year_only_without_date_is_accepted(self) -> None:
        response = self.post(self.admin, defenseDate="", defenseYear=1998)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(response.data["thesis"]["defenseDate"])
        self.assertEqual(response.data["thesis"]["defenseYear"], 1998)

    def test_advisor_with_revoked_role_is_accepted(self) -> None:
        retired = create_user("aposentado@ufvjm.edu.br", "Professor Aposentado", ProfileRole.ADVISOR)
        retired.profile_roles.update(revoked_at=timezone.now())
        self.assertEqual(self.post(self.admin, advisorId=str(retired.pk)).status_code, 201)


class LegacyImportIntegrationTests(LegacyImportTestCase):
    """Imported theses are searchable, extracted and do not interfere with the student workflow."""

    def test_imported_thesis_is_searchable_by_unlinked_author_and_year(self) -> None:
        with self.captureOnCommitCallbacks(execute=True):
            thesis_id = self.post(self.admin).data["thesis"]["id"]
        self.client.force_authenticate(user=None)
        by_author = self.client.get("/api/v1/public/theses", {"author": "fulano"})
        self.assertEqual([item["id"] for item in by_author.data["results"]], [thesis_id])
        self.assertEqual(by_author.data["results"][0]["student"], {"id": None, "name": "Fulano de Tal (2015)"})
        self.assertEqual(self.client.get("/api/v1/public/theses", {"year": 2015}).data["count"], 1)
        self.assertIn({"year": 2015, "count": 1}, self.client.get("/api/v1/public/theses/filters").data["years"])
        detail = self.client.get(f"/api/v1/public/theses/{thesis_id}").data["thesis"]
        self.assertEqual(detail["origin"], ThesisOrigin.LEGACY_IMPORT)
        self.assertEqual(len(detail["integrity"]["sha256"]), 64)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis_id}/download").status_code, 200)

    def test_pdf_goes_through_the_existing_text_extraction(self) -> None:
        thesis_id = self.post(self.admin).data["thesis"]["id"]
        stored_file = Thesis.objects.get(pk=thesis_id).files.get().stored_file
        self.assertEqual(stored_file.processing_status, ProcessingStatus.PENDING)
        self.assertEqual(extract_pending_thesis_text(limit=5), {"completed": 1, "failed": 0})
        stored_file.refresh_from_db()
        self.assertEqual(stored_file.processing_status, ProcessingStatus.COMPLETED)

    def test_linked_student_keeps_their_own_workflow_thesis(self) -> None:
        self.post(self.admin)
        self.client.force_authenticate(user=self.student)
        self.assertIsNone(self.client.get("/api/v1/theses/me").data["thesis"])
        own = create_thesis(ThesisSpec(title="TCC em andamento", course=self.course, advisor=self.advisor, authors=[self.student], status=ThesisStatus.DRAFT))
        self.assertEqual(self.client.get("/api/v1/theses/me").data["thesis"]["id"], str(own.pk))
