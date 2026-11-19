"""
Tests for ``GET /api/v1/reports/overview``: permissions, coordinator scope, filters and indicators.

Completed defenses are produced through the real close path (``close_defense`` →
``_finalize_defense``), so these tests break if the result metadata keys change there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department, UserCourseAssignment
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from reports.services import DEFENSE_ID_METADATA_KEY, DEFENSE_RESULT_METADATA_KEY
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseResult, Thesis, ThesisStatus, ThesisStatusHistory
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
OVERVIEW_URL = "/api/v1/reports/overview"


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def at(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 15, 0, tzinfo=dt_timezone.utc)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ReportsOverviewTests(TestCase):
    """Overview indicators computed in the database and scoped by role."""

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
        self.thesis_1 = Thesis.objects.create(course=self.course_a, advisor=self.advisor_1, title="T1", status=ThesisStatus.SUBMITTED)
        self.complete_defense(self.thesis_1, DefenseResult.REJECTED, at(2025, 3, 1), at(2025, 4, 1))
        self.complete_defense(self.thesis_1, DefenseResult.APPROVED, at(2025, 5, 1), at(2025, 6, 10))
        # t2: aprovada com ressalvas em 2026 (30 dias).
        self.thesis_2 = Thesis.objects.create(course=self.course_a, advisor=self.advisor_2, title="T2", status=ThesisStatus.SUBMITTED)
        self.complete_defense(self.thesis_2, DefenseResult.APPROVED_WITH_REMARKS, at(2026, 1, 10), at(2026, 2, 9))
        self.thesis_3 = Thesis.objects.create(course=self.course_b, advisor=self.advisor_1, title="T3", status=ThesisStatus.SUBMITTED)
        self.thesis_7 = Thesis.objects.create(course=self.course_b, advisor=self.advisor_1, title="T7", status=ThesisStatus.ADVISOR_APPROVED)
        schedule_defense(self.thesis_7, scheduled_datetime=timezone.now() + timedelta(days=400), actor=self.admin)
        Thesis.objects.create(course=self.course_c, advisor=self.advisor_2, title="T5", status=ThesisStatus.PUBLISHED)
        Thesis.objects.create(course=self.course_a, advisor=self.advisor_1, title="Rascunho", status=ThesisStatus.DRAFT)
        Thesis.objects.create(course=self.course_a, advisor=self.advisor_2, title="Excluída", status=ThesisStatus.SUBMITTED, deleted_at=timezone.now())
        self.client = APIClient()

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

    def overview(self, user: Any, **params: str) -> Any:
        self.client.force_authenticate(user=user)
        return self.client.get(OVERVIEW_URL, params)

    def summary(self, user: Any, **params: str) -> dict[str, Any]:
        response = self.overview(user, **params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data["summary"]

    # --- permissões ---

    def test_advisor_student_external_and_anonymous_are_denied(self) -> None:
        self.assertEqual(self.overview(self.advisor_1).status_code, 403)
        self.assertEqual(self.overview(self.student).status_code, 403)
        invitation, _token = create_external_invitation(name="Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        self.assertEqual(self.overview(ExternalInvitationPrincipal(invitation)).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(OVERVIEW_URL).status_code, (401, 403))

    # --- indicadores (admin vê tudo) ---

    def test_admin_summary_indicators(self) -> None:
        response = self.overview(self.admin)
        self.assertEqual(response.data["scope"], "ALL")
        summary = response.data["summary"]
        self.assertEqual(summary["thesisCount"], 5)
        self.assertEqual(summary["inProgressCount"], 2)
        self.assertEqual(summary["publishedCount"], 1)
        self.assertEqual(summary["scheduledDefenseCount"], 1)
        self.assertEqual(summary["completedDefenseCount"], 3)
        self.assertEqual(summary["approvedCount"], 1)
        self.assertEqual(summary["approvedWithRemarksCount"], 1)
        self.assertEqual(summary["rejectedCount"], 1)
        self.assertEqual(summary["approvalRate"], 0.6667)
        # (31 + 40 + 30) / 3: a banca reprovada usa o envio anterior a ela, não o reenvio.
        self.assertEqual(summary["averageSubmissionToDefenseDays"], 33.7)

    def test_groups_by_course_advisor_and_defense_year(self) -> None:
        data = self.overview(self.admin).data
        by_course = {row["courseCode"]: row for row in data["byCourse"]}
        self.assertEqual(set(by_course), {"CC", "SI", "ENG"})
        self.assertEqual(by_course["CC"]["thesisCount"], 2)
        self.assertEqual(by_course["CC"]["completedDefenseCount"], 3)
        self.assertEqual(by_course["SI"]["thesisCount"], 2)
        self.assertEqual(by_course["SI"]["completedDefenseCount"], 0)
        self.assertIsNone(by_course["SI"]["approvalRate"])
        self.assertIsNone(by_course["SI"]["averageSubmissionToDefenseDays"])
        self.assertEqual(by_course["ENG"]["publishedCount"], 1)
        self.assertEqual([row["advisorName"] for row in data["byAdvisor"]], ["Ana Orientadora", "Bruno Orientador"])
        self.assertEqual(data["byAdvisor"][0]["thesisCount"], 3)
        self.assertEqual(data["byAdvisor"][1]["approvedWithRemarksCount"], 1)
        by_year = {row["year"]: row for row in data["byDefenseYear"]}
        self.assertEqual(set(by_year), {2025, 2026})
        self.assertEqual(by_year[2025]["completedDefenseCount"], 2)
        self.assertEqual(by_year[2025]["approvalRate"], 0.5)
        self.assertEqual(by_year[2025]["averageSubmissionToDefenseDays"], 35.5)
        self.assertEqual(by_year[2026]["approvalRate"], 1.0)

    def test_options_list_courses_advisors_and_years(self) -> None:
        options = self.overview(self.admin).data["options"]
        self.assertEqual([course["code"] for course in options["courses"]], ["CC", "ENG", "SI"])
        self.assertEqual({advisor["name"] for advisor in options["advisors"]}, {"Ana Orientadora", "Bruno Orientador"})
        self.assertEqual(options["years"], [2025, 2026])

    def test_result_metadata_keys_match_finalize_defense(self) -> None:
        close_entries = ThesisStatusHistory.objects.filter(metadata__has_keys=[DEFENSE_ID_METADATA_KEY, DEFENSE_RESULT_METADATA_KEY])
        self.assertEqual(close_entries.count(), 3)

    # --- filtros ---

    def test_filters_by_year_course_and_advisor(self) -> None:
        by_year = self.summary(self.admin, year="2025")
        self.assertEqual(by_year["thesisCount"], 1)
        self.assertEqual(by_year["completedDefenseCount"], 2)
        self.assertEqual(by_year["scheduledDefenseCount"], 0)
        by_course = self.summary(self.admin, courseId=str(self.course_b.pk))
        self.assertEqual(by_course["thesisCount"], 2)
        self.assertEqual(by_course["scheduledDefenseCount"], 1)
        by_advisor = self.summary(self.admin, advisorId=str(self.advisor_2.pk))
        self.assertEqual(by_advisor["thesisCount"], 2)
        self.assertEqual(by_advisor["completedDefenseCount"], 1)

    def test_invalid_filters_return_400(self) -> None:
        self.assertEqual(self.overview(self.admin, courseId="abc").status_code, 400)
        self.assertEqual(self.overview(self.admin, advisorId="abc").status_code, 400)
        self.assertEqual(self.overview(self.admin, year="ano").status_code, 400)
        self.assertEqual(self.overview(self.admin, year="1500").status_code, 400)
        response = self.overview(self.admin, scope="everything")
        self.assertEqual(response.status_code, 400)
        self.assertIn("scope", response.data)

    # --- escopo do coordenador ---

    def test_coordinator_sees_only_courses_linked_by_fk_email_or_assignment(self) -> None:
        response = self.overview(self.coordinator_a)
        self.assertEqual(response.data["scope"], "COORDINATED_COURSES")
        self.assertEqual(response.data["summary"]["thesisCount"], 2)
        self.assertEqual(response.data["summary"]["completedDefenseCount"], 3)
        self.assertEqual([course["code"] for course in response.data["options"]["courses"]], ["CC"])
        self.assertEqual(self.summary(self.coordinator_b)["thesisCount"], 2)
        coordinator_c_summary = self.summary(self.coordinator_c)
        self.assertEqual(coordinator_c_summary["thesisCount"], 1)
        self.assertEqual(coordinator_c_summary["publishedCount"], 1)

    def test_coordinator_course_filter_outside_scope_returns_empty_aggregates(self) -> None:
        response = self.overview(self.coordinator_a, courseId=str(self.course_b.pk))
        self.assertEqual(response.data["summary"]["thesisCount"], 0)
        self.assertEqual(response.data["byCourse"], [])

    def test_coordinator_report_ignores_theses_advised_in_other_courses(self) -> None:
        UserProfileRole.objects.create(user=self.coordinator_a, role=ProfileRole.ADVISOR)
        Thesis.objects.create(course=self.course_b, advisor=self.coordinator_a, title="Orientada fora", status=ThesisStatus.SUBMITTED)
        self.assertEqual(self.summary(self.coordinator_a)["thesisCount"], 2)

    def test_admin_scope_coordinated_restricts_to_own_courses(self) -> None:
        response = self.overview(self.admin, scope="coordinated")
        self.assertEqual(response.data["scope"], "COORDINATED_COURSES")
        self.assertEqual(response.data["summary"]["thesisCount"], 0)
        UserProfileRole.objects.create(user=self.coordinator_a, role=ProfileRole.ADMIN)
        self.assertEqual(self.summary(self.coordinator_a)["thesisCount"], 5)
        self.assertEqual(self.summary(self.coordinator_a, scope="coordinated")["thesisCount"], 2)

    # --- desempenho ---

    def test_query_count_does_not_grow_with_data(self) -> None:
        self.client.force_authenticate(user=self.admin)
        with CaptureQueriesContext(connection) as first_run:
            self.client.get(OVERVIEW_URL)
        extra_course = Course.objects.create(department=self.department, name="Matemática", code="MAT")
        for index in range(5):
            Thesis.objects.create(course=extra_course, advisor=self.evaluator, title=f"Extra {index}", status=ThesisStatus.SUBMITTED)
        cache.clear()
        with CaptureQueriesContext(connection) as second_run:
            response = self.client.get(OVERVIEW_URL)
        self.assertEqual(response.data["summary"]["thesisCount"], 10)
        self.assertEqual(len(second_run), len(first_run))

    def test_response_is_cached_per_scope_and_filters(self) -> None:
        first = self.summary(self.admin)
        Thesis.objects.create(course=self.course_a, advisor=self.advisor_1, title="Nova", status=ThesisStatus.SUBMITTED)
        self.assertEqual(self.summary(self.admin)["thesisCount"], first["thesisCount"])
        self.assertEqual(self.summary(self.admin, courseId=str(self.course_a.pk))["thesisCount"], 3)
