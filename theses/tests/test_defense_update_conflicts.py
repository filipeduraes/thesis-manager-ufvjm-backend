"""
Tests for committee schedule conflict checks when updating a defense.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, Defense, DefenseMember, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseUpdateConflictApiTests(TestCase):
    """PATCH /api/v1/defenses/<id> must honor committee member availability."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.member_user = create_user("member@ufvjm.edu.br", "Profa. Membro", ProfileRole.ADVISOR)

        # Banca A: daqui 7 dias, 120 min. Banca B: 3 horas depois do início de A (sem sobreposição).
        self.start = (timezone.now() + timedelta(days=7)).replace(microsecond=0)
        thesis_a = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese A", status=ThesisStatus.ADVISOR_APPROVED)
        thesis_b = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese B", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense_a = schedule_defense(thesis_a, scheduled_datetime=self.start, actor=self.admin)
        self.defense_b = schedule_defense(thesis_b, scheduled_datetime=self.start + timedelta(hours=3), actor=self.admin)
        add_institutional_member(self.defense_a, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member_user, actor=self.admin)

        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _patch(self, defense: Defense, payload: dict[str, object]):
        return self.client.patch(f"/api/v1/defenses/{defense.pk}", payload, format="json")

    def test_reschedule_without_conflict_succeeds(self) -> None:
        new_start = self.start + timedelta(days=1)
        response = self._patch(self.defense_b, {"scheduledDatetime": new_start.isoformat()})
        self.assertEqual(response.status_code, 200)
        self.defense_b.refresh_from_db()
        self.assertEqual(self.defense_b.scheduled_datetime, new_start)

    def test_adjacent_reschedule_succeeds(self) -> None:
        response = self._patch(self.defense_b, {"scheduledDatetime": (self.start + timedelta(minutes=120)).isoformat()})
        self.assertEqual(response.status_code, 200)

    def test_reschedule_into_member_conflict_returns_400(self) -> None:
        response = self._patch(self.defense_b, {"scheduledDatetime": (self.start + timedelta(minutes=30)).isoformat()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "O membro Profa. Membro já participa de outra banca no mesmo período.")
        self.defense_b.refresh_from_db()
        self.assertEqual(self.defense_b.scheduled_datetime, self.start + timedelta(hours=3))

    def test_duration_change_into_member_conflict_returns_400(self) -> None:
        response = self._patch(self.defense_a, {"durationMinutes": 240})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Profa. Membro", response.data["detail"])
        self.defense_a.refresh_from_db()
        self.assertEqual(self.defense_a.duration_minutes, 120)

    def test_location_or_notes_change_skips_conflict_check(self) -> None:
        with patch("theses.defense_scheduling.check_committee_schedule_conflicts") as conflict_check:
            response = self._patch(self.defense_b, {"location": "Sala 202", "notes": "Levar projetor"})
        self.assertEqual(response.status_code, 200)
        conflict_check.assert_not_called()
        self.assertEqual(response.data["defense"]["location"], "Sala 202")

    def test_location_change_allowed_even_with_preexisting_overlap(self) -> None:
        # Sobreposição criada fora do fluxo normal (dados legados): editar só o local não deve bloquear.
        Defense.objects.filter(pk=self.defense_b.pk).update(scheduled_datetime=self.start + timedelta(minutes=30))
        response = self._patch(self.defense_b, {"location": "Auditório"})
        self.assertEqual(response.status_code, 200)

    def test_external_members_are_not_checked(self) -> None:
        DefenseMember.objects.filter(defense=self.defense_b).delete()
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        DefenseMember.objects.create(defense=self.defense_b, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        response = self._patch(self.defense_b, {"scheduledDatetime": (self.start + timedelta(minutes=30)).isoformat()})
        self.assertEqual(response.status_code, 200)
