"""Course-scope and state guards on the manual publication chain."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_scheduling import schedule_defense
from theses.models import (
    ACTIVE_DEFENSE_STATUSES,
    Defense,
    DefenseResult,
    DocumentStatus,
    DocumentType,
    StoredFile,
    Thesis,
    ThesisAuthor,
    ThesisDocument,
    ThesisStatus,
)
from theses.services import publish_thesis, record_defense_result, validate_thesis_document

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=email.split("@")[0])
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class PublicationChainScopeTests(TestCase):
    """A coordinator cannot manage a thesis of another course, even as its advisor."""

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.coordinator_a = create_user("coord.a@ufvjm.edu.br", ProfileRole.COORDINATOR)
        self.coordinator_b = create_user("coord.b@ufvjm.edu.br", ProfileRole.COORDINATOR)
        self.course_a = Course.objects.create(department=department, coordinator=self.coordinator_a, name="CC", code="CC1")
        self.course_b = Course.objects.create(department=department, coordinator=self.coordinator_b, name="SI", code="SI1")
        self.student = create_user("aluno@ufvjm.edu.br", ProfileRole.STUDENT)
        self.advisor = User.objects.create_user(email="orientador@ufvjm.edu.br", name="Orientador")
        UserProfileRole.objects.create(user=self.advisor, role=ProfileRole.ADVISOR)
        # O mesmo usuário é coordenador do curso A e orientador de uma tese do curso B.
        UserProfileRole.objects.create(user=self.advisor, role=ProfileRole.COORDINATOR)
        self.thesis = Thesis.objects.create(course_id=self.course_b.pk, advisor_id=self.advisor.pk, title="Tese do curso B", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author_id=self.student.pk, position=1, is_main_author=True)

    def test_cross_course_coordinator_cannot_record_manual_result(self) -> None:
        with self.assertRaises(PermissionDenied):
            record_defense_result(self.advisor, self.thesis, DefenseResult.APPROVED)

    def test_cross_course_coordinator_cannot_publish(self) -> None:
        with self.assertRaises(PermissionDenied):
            publish_thesis(self.advisor, self.thesis)

    def test_cross_course_coordinator_cannot_validate_document(self) -> None:
        stored_file = StoredFile.objects.create(
            original_filename="autorizacao.pdf",
            stored_filename="autorizacao.pdf",
            size_bytes=4,
            sha256=hashlib.sha256(b"test").digest(),
            storage_path="thesis-documents/autorizacao.pdf",
            uploaded_by_id=self.student.pk,
        )
        document = ThesisDocument.objects.create(
            thesis=self.thesis,
            stored_file=stored_file,
            type=DocumentType.PUBLICATION_AUTHORIZATION,
            status=DocumentStatus.SUBMITTED,
            submitted_by_id=self.student.pk,
        )
        with self.assertRaises(PermissionDenied):
            validate_thesis_document(self.advisor, self.thesis, document)

    def test_manual_result_is_rejected_for_published_thesis(self) -> None:
        Thesis.objects.filter(pk=self.thesis.pk).update(status=ThesisStatus.PUBLISHED, is_public=True)
        self.thesis.refresh_from_db()
        with self.assertRaises(ValidationError):
            record_defense_result(self.coordinator_b, self.thesis, DefenseResult.APPROVED)

    def test_manual_result_is_rejected_while_a_defense_is_active(self) -> None:
        schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=3), actor=self.coordinator_b)
        self.assertTrue(Defense.objects.filter(thesis_id=self.thesis.pk, status__in=ACTIVE_DEFENSE_STATUSES).exists())
        with self.assertRaises(ValidationError):
            record_defense_result(self.coordinator_b, self.thesis, DefenseResult.APPROVED)
