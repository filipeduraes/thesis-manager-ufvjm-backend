"""
Tests for meeting link validation of remote defenses.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_scheduling import REMOTE_LOCATION_ERROR, schedule_defense
from theses.models import Defense, DefenseModality, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class RemoteDefenseLocationTests(TestCase):
    """REMOTE requires an http(s) link; IN_PERSON keeps free text; legacy data stays editable."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.client = APIClient()
        self.client.force_authenticate(user=self.advisor)
        self.when = (timezone.now() + timedelta(days=7)).isoformat()

    def _schedule(self, payload: dict[str, str]):
        body = {"scheduledDatetime": self.when, **payload}
        return self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", body, format="json")

    def _patch(self, defense: Defense, payload: dict[str, str]):
        return self.client.patch(f"/api/v1/defenses/{defense.pk}", payload, format="json")

    # --- agendamento ---

    def test_remote_with_https_link_is_accepted(self) -> None:
        response = self._schedule({"modality": DefenseModality.REMOTE, "location": "https://meet.example.com/abc-defg"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["defense"]["location"], "https://meet.example.com/abc-defg")

    def test_remote_with_http_link_is_accepted(self) -> None:
        response = self._schedule({"modality": DefenseModality.REMOTE, "location": "http://conferencia.ufvjm.edu.br/sala1"})
        self.assertEqual(response.status_code, 201)

    def test_remote_without_location_is_rejected(self) -> None:
        response = self._schedule({"modality": DefenseModality.REMOTE})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], REMOTE_LOCATION_ERROR)
        self.assertIn("location", response.data["errors"])
        self.assertFalse(Defense.objects.filter(thesis=self.thesis).exists())

    def test_remote_with_free_text_is_rejected(self) -> None:
        for location in ("Sala 101", "meet.example.com/abc", "ftp://example.com/x", "javascript:alert(1)"):
            response = self._schedule({"modality": DefenseModality.REMOTE, "location": location})
            self.assertEqual(response.status_code, 400, location)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.ADVISOR_APPROVED)

    def test_in_person_keeps_free_text_and_empty_location(self) -> None:
        response = self._schedule({"modality": DefenseModality.IN_PERSON, "location": "Auditório do prédio 2"})
        self.assertEqual(response.status_code, 201)

    def test_default_modality_accepts_empty_location(self) -> None:
        response = self._schedule({})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["defense"]["modality"], DefenseModality.IN_PERSON)

    # --- PATCH ---

    def test_patch_to_remote_requires_link(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), location="Sala 1", actor=self.advisor)
        response = self._patch(defense, {"modality": DefenseModality.REMOTE})
        self.assertEqual(response.status_code, 400)
        response = self._patch(defense, {"modality": DefenseModality.REMOTE, "location": "https://meet.example.com/x"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["modality"], DefenseModality.REMOTE)

    def test_patch_invalid_link_on_remote_defense_is_rejected(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location="https://meet.example.com/x", actor=self.advisor)
        response = self._patch(defense, {"location": "Sala 3"})
        self.assertEqual(response.status_code, 400)
        defense.refresh_from_db()
        self.assertEqual(defense.location, "https://meet.example.com/x")

    def test_patch_back_to_in_person_accepts_free_text(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location="https://meet.example.com/x", actor=self.advisor)
        response = self._patch(defense, {"modality": DefenseModality.IN_PERSON, "location": "Sala 3"})
        self.assertEqual(response.status_code, 200)

    def test_legacy_remote_defense_with_free_text_stays_editable(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), location="Sala 1", actor=self.advisor)
        # Banca antiga: remota com local fora do padrão (gravada antes da validação).
        Defense.objects.filter(pk=defense.pk).update(modality=DefenseModality.REMOTE, location="Link enviado por e-mail")
        response = self._patch(defense, {"notes": "Atualizado", "durationMinutes": 90})
        self.assertEqual(response.status_code, 200)
        response = self._patch(defense, {"modality": DefenseModality.REMOTE, "location": "Link enviado por e-mail"})
        self.assertEqual(response.status_code, 200)
        response = self._patch(defense, {"location": "Outro texto"})
        self.assertEqual(response.status_code, 400)
