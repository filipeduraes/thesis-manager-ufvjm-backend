"""
Shared report scenario: courses linked to coordinators in three ways, theses and
completed defenses produced through the real close path (``close_defense`` →
``_finalize_defense``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department, UserCourseAssignment
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseResult, Thesis, ThesisStatus, ThesisStatusHistory
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 15, 0, tzinfo=dt_timezone.utc)


class ReportScenarioMixin:
    """Builds the dataset used by every reports test (mixed into ``TestCase``)."""

    def setUp(self) -> None:
        cache.clear()
        self.slot = 0
        self.department = Department.objects.create(name="Computação")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.coordinator_a = create_user("coord.a@ufvjm.edu.br", "Coord A", ProfileRole.COORDINATOR)
        self.coordinator_b = create_user("coord.b@ufvjm.edu.br", "Coord B", ProfileRole.COORDINATOR)
        self.coordinator_c = create_user("coord.c@ufvjm.edu.br", "Coord C", ProfileRole.COORDINATOR)
        self.advisor_1 = create_user("ana@ufvjm.edu.br", "Ana Orientadora", ProfileRole.ADVISOR)
        self.advisor_2 = create_user("bruno@ufvjm.edu.br", "Bruno Orientador", ProfileRole.ADVISOR)
        self.evaluator = create_user("avaliador@ufvjm.edu.br", "Avaliador", ProfileRole.ADVISOR)
        self.student = create_user("aluno@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        # Três formas de vínculo do coordenador: FK, e-mail e associação ativa.
        self.course_a = Course.objects.create(department=self.department, coordinator=self.coordinator_a, name="Ciência da Computação", code="CC")
        self.course_b = Course.objects.create(department=self.department, coordinator_email="coord.b@ufvjm.edu.br", name="Sistemas de Informação", code="SI")
        self.course_c = Course.objects.create(department=self.department, name="Engenharia", code="ENG")
        UserCourseAssignment.objects.create(user=self.coordinator_c, course=self.course_c)

        # t1: reprovada em 2025 (31 dias), reenviada e aprovada em 2025 (40 dias).
        self.thesis_1 = Thesis.objects.create(**self.snapshot(self.course_a, self.advisor_1), course_id=self.course_a.pk, advisor_id=self.advisor_1.pk, title="T1", status=ThesisStatus.SUBMITTED)
        self.complete_defense(self.thesis_1, DefenseResult.REJECTED, at(2025, 3, 1), at(2025, 4, 1))
        self.complete_defense(self.thesis_1, DefenseResult.APPROVED, at(2025, 5, 1), at(2025, 6, 10))
        # t2: aprovada com ressalvas em 2026 (30 dias).
        self.thesis_2 = Thesis.objects.create(**self.snapshot(self.course_a, self.advisor_2), course_id=self.course_a.pk, advisor_id=self.advisor_2.pk, title="T2", status=ThesisStatus.SUBMITTED)
        self.complete_defense(self.thesis_2, DefenseResult.APPROVED_WITH_REMARKS, at(2026, 1, 10), at(2026, 2, 9))
        self.thesis_3 = Thesis.objects.create(**self.snapshot(self.course_b, self.advisor_1), course_id=self.course_b.pk, advisor_id=self.advisor_1.pk, title="T3", status=ThesisStatus.SUBMITTED)
        self.thesis_7 = Thesis.objects.create(**self.snapshot(self.course_b, self.advisor_1), course_id=self.course_b.pk, advisor_id=self.advisor_1.pk, title="T7", status=ThesisStatus.ADVISOR_APPROVED)
        schedule_defense(self.thesis_7, scheduled_datetime=timezone.now() + timedelta(days=400), actor=self.admin)
        Thesis.objects.create(**self.snapshot(self.course_c, self.advisor_2), course_id=self.course_c.pk, advisor_id=self.advisor_2.pk, title="T5", status=ThesisStatus.PUBLISHED)
        Thesis.objects.create(**self.snapshot(self.course_a, self.advisor_1), course_id=self.course_a.pk, advisor_id=self.advisor_1.pk, title="Rascunho", status=ThesisStatus.DRAFT)
        Thesis.objects.create(**self.snapshot(self.course_a, self.advisor_2), course_id=self.course_a.pk, advisor_id=self.advisor_2.pk, title="Excluída", status=ThesisStatus.SUBMITTED, deleted_at=timezone.now())
        self.client = APIClient()

    @staticmethod
    def snapshot(course, advisor) -> dict:
        """Local identity fields used by report aggregations."""
        return {
            "advisor_name": advisor.name or advisor.email,
            "course_name": course.name,
            "course_code": course.code,
            "main_author_name": "Aluno de teste",
        }

    def complete_defense(self, thesis: Thesis, decision: str, submitted_at: datetime, defense_at: datetime) -> Defense:
        """Run a defense through schedule → start → evaluation → close, then move it to ``defense_at``."""
        ThesisStatusHistory.objects.create(thesis=thesis, previous_status=ThesisStatus.DRAFT, new_status=ThesisStatus.SUBMITTED, changed_at=submitted_at)
        Thesis.objects.filter(pk=thesis.pk).update(status=ThesisStatus.ADVISOR_APPROVED, submitted_at=submitted_at)
        thesis.refresh_from_db()
        self.slot += 1
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=30 + self.slot * 3), actor=self.admin)
        member = add_institutional_member(defense, CommitteeRole.PRESIDENT, self.evaluator, actor=self.admin)
        confirm_committee(defense)
        start_defense(defense, actor=self.admin)
        record_evaluation_without_auto_close(member, decision)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(defense, actor=self.admin)
        Defense.objects.filter(pk=defense.pk).update(scheduled_datetime=defense_at)
        return defense
