"""
Regression tests: writes must not assign model instances from other services.

The strict router reproduces the error Postgres raises when a thesis row tries
to reference a User/Course object during ``Model(... )`` construction.
"""

from __future__ import annotations

from datetime import date, timedelta

from django.test import TestCase, override_settings
import tempfile
from pathlib import Path
from django.utils import timezone

from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, ThesisStatus
from theses.services import review_thesis, save_thesis_metadata, submit_thesis, upload_thesis_document, upload_thesis_file, validate_thesis_document
from theses.tests.test_api import create_user, pdf_upload
from thesismanagerbackend.routers import ServiceDatabaseRouter


class StrictRelationRouter(ServiceDatabaseRouter):
    """Reject every model-to-model relation assignment."""

    def allow_relation(self, obj1, obj2, **hints) -> bool:
        first = self.db_for_write(type(obj1)) or "default"
        second = self.db_for_write(type(obj2)) or "default"
        return first == second


@override_settings(DATABASE_ROUTERS=["theses.tests.test_cross_service_assignments.StrictRelationRouter"])
class CrossServiceAssignmentTests(TestCase):
    """Use temporary media because production MEDIA_ROOT may be read-only here."""

    def setUp(self) -> None:
        self.media_root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.media_root, True)
        media = override_settings(MEDIA_ROOT=Path(self.media_root))
        media.enable()
        self.addCleanup(media.disable)
        from academic.models import Course, Department

        self.admin = create_user("admin@ufvjm.edu.br", "Admin", "ADMIN")
        self.advisor = create_user("adv@ufvjm.edu.br", "Orientador", "ADVISOR")
        self.student = create_user("aluno@ufvjm.edu.br", "Aluno", "STUDENT")
        department = Department.objects.create(name="Computação")
        self.course = Course.objects.create(department=department, name="Computação", code="CC-X")

    def test_save_thesis_metadata_uses_foreign_keys(self) -> None:
        thesis = save_thesis_metadata(self.student, self.course, self.advisor, "Título", "Resumo", ["teste"])
        self.assertEqual(thesis.course_id, self.course.pk)
        self.assertEqual(thesis.advisor_id, self.advisor.pk)

    def test_defense_and_member_creation_use_foreign_keys(self) -> None:
        thesis = save_thesis_metadata(self.student, self.course, self.advisor, "Título", "Resumo", ["teste"])
        thesis.status = ThesisStatus.ADVISOR_APPROVED
        thesis.save(update_fields=["status"])
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.assertEqual(defense.created_by_id, self.admin.pk)
        member = add_institutional_member(defense, CommitteeRole.PRESIDENT, self.advisor, actor=self.admin)
        self.assertEqual(member.institutional_member_id, self.advisor.pk)

    def test_review_document_and_publication_use_foreign_keys(self) -> None:
        thesis = save_thesis_metadata(self.student, self.course, self.advisor, "Título", "Resumo", ["teste"])
        file = upload_thesis_file(self.student, thesis, pdf_upload())
        self.assertEqual(file.uploaded_by_id, self.student.pk)
        submit_thesis(self.student, thesis)
        review = review_thesis(self.advisor, thesis, "APPROVED", "ok")
        self.assertEqual(review.reviewer_id, self.advisor.pk)
        document = upload_thesis_document(self.admin, thesis, "PUBLICATION_AUTHORIZATION", pdf_upload("aut.pdf"))
        validated = validate_thesis_document(self.admin, thesis, document)
        self.assertEqual(validated.validated_by_id, self.admin.pk)
