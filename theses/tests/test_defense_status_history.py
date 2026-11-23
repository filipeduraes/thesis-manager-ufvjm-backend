"""
Tests for the defense status history (DefenseStatusHistory) and its API exposure.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import cancel_defense, close_defense, revert_defense_status, schedule_defense, start_defense, sync_defense_status
from theses.models import CommitteeRole, Defense, DefenseMemberEvaluation, DefenseResult, DefenseStatus, DefenseStatusHistory, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseStatusHistoryTests(TestCase):
    """Each defense status transition must append exactly one history entry."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.member_user = create_user("member@ufvjm.edu.br", "Membro", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.advisor)
        self.client = APIClient()

    def _entries(self) -> list[DefenseStatusHistory]:
        return list(DefenseStatusHistory.objects.filter(defense=self.defense).order_by("created_at"))

    def _last_entry(self) -> DefenseStatusHistory:
        return self._entries()[-1]

    def test_schedule_records_creation(self) -> None:
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].previous_status, "")
        self.assertEqual(entries[0].new_status, DefenseStatus.SCHEDULED)
        self.assertEqual(entries[0].changed_by, self.advisor)
        self.assertEqual(entries[0].reason, "Banca agendada.")

    def test_manual_start_records_actor(self) -> None:
        start_defense(self.defense, actor=self.admin)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.SCHEDULED, DefenseStatus.IN_PROGRESS))
        self.assertEqual(entry.changed_by, self.admin)
        self.assertEqual(entry.reason, "Banca iniciada.")

    def test_auto_start_records_null_actor(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=1))
        self.defense.refresh_from_db()
        sync_defense_status(self.defense)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.SCHEDULED, DefenseStatus.IN_PROGRESS))
        self.assertIsNone(entry.changed_by)
        self.assertEqual(entry.reason, "Banca iniciada automaticamente pelo horário agendado.")

    def test_auto_start_is_recorded_once(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=1))
        self.defense.refresh_from_db()
        sync_defense_status(self.defense)
        sync_defense_status(self.defense)
        self.assertEqual(len(self._entries()), 2)

    def test_close_records_result(self) -> None:
        member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        DefenseMemberEvaluation.objects.create(member=member, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.")
        close_defense(self.defense, actor=self.coordinator)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.IN_PROGRESS, DefenseStatus.COMPLETED))
        self.assertEqual(entry.changed_by, self.coordinator)
        self.assertEqual(entry.reason, f"Banca encerrada. Resultado: {DefenseResult.REJECTED.label}.")

    def test_cancel_records_reason(self) -> None:
        cancel_defense(self.defense, reason="Aluno doente.", actor=self.advisor)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.SCHEDULED, DefenseStatus.CANCELLED))
        self.assertEqual(entry.changed_by, self.advisor)
        self.assertEqual(entry.reason, "Banca cancelada. Aluno doente.")

    def test_revert_in_progress_records_reason(self) -> None:
        start_defense(self.defense, actor=self.admin)
        revert_defense_status(self.defense, reason="Iniciada por engano.", actor=self.admin)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.IN_PROGRESS, DefenseStatus.SCHEDULED))
        self.assertEqual(entry.changed_by, self.admin)
        self.assertEqual(entry.reason, "Status revertido pelo administrador. Iniciada por engano.")

    def test_revert_cancelled_records_reason(self) -> None:
        cancel_defense(self.defense, actor=self.admin)
        revert_defense_status(self.defense, reason="Cancelada por engano.", actor=self.admin)
        entry = self._last_entry()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.CANCELLED, DefenseStatus.SCHEDULED))
        self.assertIn("Cancelada por engano.", entry.reason)

    def test_failed_transition_records_nothing(self) -> None:
        with self.assertRaises(ValidationError):
            close_defense(self.defense, actor=self.admin)
        self.assertEqual(len(self._entries()), 1)

    def test_detail_returns_status_history_in_order(self) -> None:
        start_defense(self.defense, actor=self.admin)
        revert_defense_status(self.defense, reason="Engano.", actor=self.admin)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.status_code, 200)
        history = response.data["defense"]["statusHistory"]
        self.assertEqual([item["newStatus"] for item in history], [DefenseStatus.SCHEDULED, DefenseStatus.IN_PROGRESS, DefenseStatus.SCHEDULED])
        first = history[0]
        self.assertIsNone(first["previousStatus"])
        self.assertIsNone(first["previousStatusLabel"])
        self.assertEqual(first["newStatusLabel"], DefenseStatus.SCHEDULED.label)
        self.assertEqual(first["changedBy"], "Orientador")
        self.assertEqual(first["reason"], "Banca agendada.")
        self.assertIn("createdAt", first)
        self.assertEqual(history[1]["previousStatus"], DefenseStatus.SCHEDULED)
        self.assertEqual(history[1]["previousStatusLabel"], DefenseStatus.SCHEDULED.label)
        self.assertEqual(history[2]["changedBy"], "Admin")

    def test_detail_auto_start_history_has_null_changed_by(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=1))
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        last = response.data["defense"]["statusHistory"][-1]
        self.assertEqual(last["newStatus"], DefenseStatus.IN_PROGRESS)
        self.assertIsNone(last["changedBy"])

    def test_listing_does_not_include_status_history(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/defenses")
        self.assertNotIn("statusHistory", response.data["results"][0])
