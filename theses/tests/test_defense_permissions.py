"""
API tests for who may start, close and revert a defense.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseMemberEvaluation, DefenseResult, DefenseStatus, DefenseStatusHistory, Thesis, ThesisAuthor, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseStartClosePermissionTests(TestCase):
    """Admin, coordinator and the thesis advisor may start/close; revert stays admin-only."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.unrelated_advisor = create_user("unrelated@ufvjm.edu.br", "Sem Vínculo", ProfileRole.ADVISOR)
        self.student = create_user("student@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author=self.student, is_main_author=True)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        # Orientador de outra tese que integra esta banca: enxerga a banca, mas não a gerencia.
        self.member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.other_advisor, actor=self.admin)
        confirm_committee(self.defense)
        self.client = APIClient()

    def _post(self, user: User, action: str, payload: dict[str, str] | None = None):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/api/v1/defenses/{self.defense.pk}/{action}", payload or {}, format="json")

    def _start_and_evaluate(self) -> None:
        start_defense(self.defense, actor=self.admin)
        # REJECTED evita purge_thesis_versions, que exige arquivo atual.
        DefenseMemberEvaluation.objects.create(member=self.member, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.")

    def test_advisor_of_own_thesis_starts_defense(self) -> None:
        response = self._post(self.advisor, "start")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)
        last_entry = DefenseStatusHistory.objects.filter(defense=self.defense).order_by("created_at").last()
        self.assertEqual(last_entry.changed_by, self.advisor)

    def test_advisor_of_own_thesis_closes_defense(self) -> None:
        self._start_and_evaluate()
        response = self._post(self.advisor, "close")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.COMPLETED)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.REJECTED)

    def test_coordinator_still_starts_and_closes(self) -> None:
        response = self._post(self.coordinator, "start")
        self.assertEqual(response.status_code, 200)
        DefenseMemberEvaluation.objects.create(member=self.member, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.")
        response = self._post(self.coordinator, "close")
        self.assertEqual(response.status_code, 200)

    def test_advisor_of_other_thesis_cannot_start(self) -> None:
        response = self._post(self.other_advisor, "start")
        self.assertEqual(response.status_code, 403)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.SCHEDULED)

    def test_advisor_of_other_thesis_cannot_close(self) -> None:
        self._start_and_evaluate()
        response = self._post(self.other_advisor, "close")
        self.assertEqual(response.status_code, 403)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.IN_PROGRESS)

    def test_unrelated_advisor_gets_404_like_other_defense_endpoints(self) -> None:
        response = self._post(self.unrelated_advisor, "start")
        self.assertEqual(response.status_code, 404)

    def test_student_cannot_start(self) -> None:
        response = self._post(self.student, "start")
        self.assertEqual(response.status_code, 403)

    def test_revert_forbidden_for_coordinator_and_advisor(self) -> None:
        start_defense(self.defense, actor=self.admin)
        for user in (self.coordinator, self.advisor):
            response = self._post(user, "revert-status", {"reason": "Tentativa"})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(Defense.objects.get(pk=self.defense.pk).status, DefenseStatus.IN_PROGRESS)
