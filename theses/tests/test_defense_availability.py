"""
Tests for committee availability confirmation and its effect on starting a defense.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditLog, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import AvailabilityStatus, CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseAvailabilityTests(TestCase):
    """Members confirm their own availability; starting requires every evaluating member confirmed."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.president_user = create_user("pres@ufvjm.edu.br", "Profa. Presidente", ProfileRole.ADVISOR)
        self.member_user = create_user("member@ufvjm.edu.br", "Prof. Membro", ProfileRole.ADVISOR)
        self.alternate_user = create_user("alt@ufvjm.edu.br", "Prof. Suplente", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.start = (timezone.now() + timedelta(days=7)).replace(microsecond=0)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=self.start, actor=self.admin)
        self.president = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.president_user, actor=self.admin)
        self.member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        self.alternate = add_institutional_member(self.defense, CommitteeRole.ALTERNATE, self.alternate_user, actor=self.admin)
        self.client = APIClient()

    def _availability_url(self, member: DefenseMember) -> str:
        return f"/api/v1/defenses/{self.defense.pk}/members/{member.pk}/availability"

    def _respond(self, principal, member: DefenseMember, status: str, note: str = ""):
        self.client.force_authenticate(user=principal)
        return self.client.post(self._availability_url(member), {"status": status, "note": note}, format="json")

    def _external_member(self) -> tuple[DefenseMember, ExternalInvitationPrincipal]:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        return member, ExternalInvitationPrincipal(invitation)

    # --- resposta do membro ---

    def test_new_members_start_pending(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        members = response.data["defense"]["members"]
        self.assertTrue(all(item["availabilityStatus"] == AvailabilityStatus.PENDING for item in members))
        self.assertTrue(all(item["availabilityRespondedAt"] is None for item in members))
        self.assertEqual(members[0]["availabilityStatusLabel"], AvailabilityStatus.PENDING.label)

    def test_institutional_member_confirms(self) -> None:
        response = self._respond(self.member_user, self.member, AvailabilityStatus.CONFIRMED, "Confirmo presença.")
        self.assertEqual(response.status_code, 200)
        payload = response.data["member"]
        self.assertEqual(payload["availabilityStatus"], AvailabilityStatus.CONFIRMED)
        self.assertEqual(payload["availabilityStatusLabel"], AvailabilityStatus.CONFIRMED.label)
        self.assertEqual(payload["availabilityNote"], "Confirmo presença.")
        self.assertIsNotNone(payload["availabilityRespondedAt"])
        entry = AuditLog.objects.filter(metadata__event="AVAILABILITY_RESPONDED").get()
        self.assertEqual(entry.metadata["memberId"], str(self.member.pk))
        self.assertEqual(entry.metadata["to"], AvailabilityStatus.CONFIRMED)
        self.assertNotIn("Confirmo presença.", str(entry.metadata))

    def test_external_member_confirms_with_own_session(self) -> None:
        external_member, principal = self._external_member()
        response = self._respond(principal, external_member, AvailabilityStatus.CONFIRMED)
        self.assertEqual(response.status_code, 200)
        external_member.refresh_from_db()
        self.assertEqual(external_member.availability_status, AvailabilityStatus.CONFIRMED)
        entry = AuditLog.objects.filter(metadata__event="AVAILABILITY_RESPONDED").get()
        self.assertEqual(entry.actor_external_invitation_id, principal.invitation.pk)

    def test_member_declines(self) -> None:
        response = self._respond(self.member_user, self.member, AvailabilityStatus.DECLINED, "Viagem.")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["member"]["availabilityStatus"], AvailabilityStatus.DECLINED)

    def test_member_cannot_answer_for_another_member(self) -> None:
        response = self._respond(self.member_user, self.president, AvailabilityStatus.CONFIRMED)
        self.assertEqual(response.status_code, 403)
        self.president.refresh_from_db()
        self.assertEqual(self.president.availability_status, AvailabilityStatus.PENDING)

    def test_external_member_cannot_answer_for_institutional_member(self) -> None:
        _external_member, principal = self._external_member()
        response = self._respond(principal, self.member, AvailabilityStatus.CONFIRMED)
        self.assertEqual(response.status_code, 403)

    def test_non_member_manager_cannot_answer(self) -> None:
        for user in (self.admin, self.coordinator, self.advisor):
            response = self._respond(user, self.member, AvailabilityStatus.CONFIRMED)
            self.assertEqual(response.status_code, 403)

    def test_pending_is_not_a_valid_answer(self) -> None:
        response = self._respond(self.member_user, self.member, AvailabilityStatus.PENDING)
        self.assertEqual(response.status_code, 400)

    def test_answer_requires_scheduled_defense(self) -> None:
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        response = self._respond(self.member_user, self.member, AvailabilityStatus.DECLINED)
        self.assertEqual(response.status_code, 400)

    # --- PATCH ---

    def _patch(self, payload: dict[str, object]):
        self.client.force_authenticate(user=self.admin)
        return self.client.patch(f"/api/v1/defenses/{self.defense.pk}", payload, format="json")

    def _statuses(self) -> set[str]:
        return set(DefenseMember.objects.filter(defense=self.defense).values_list("availability_status", flat=True))

    def test_patch_location_or_notes_keeps_confirmations(self) -> None:
        confirm_committee(self.defense)
        response = self._patch({"location": "Sala 9", "notes": "Projetor"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), {AvailabilityStatus.CONFIRMED})

    def test_patch_with_same_datetime_and_duration_keeps_confirmations(self) -> None:
        confirm_committee(self.defense)
        response = self._patch({"scheduledDatetime": self.start.isoformat(), "durationMinutes": 120})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), {AvailabilityStatus.CONFIRMED})

    def test_patch_new_datetime_resets_confirmations(self) -> None:
        confirm_committee(self.defense)
        response = self._patch({"scheduledDatetime": (self.start + timedelta(days=1)).isoformat()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), {AvailabilityStatus.PENDING})
        self.assertFalse(DefenseMember.objects.filter(defense=self.defense, availability_responded_at__isnull=False).exists())

    def test_patch_new_duration_resets_confirmations(self) -> None:
        confirm_committee(self.defense)
        response = self._patch({"durationMinutes": 90})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._statuses(), {AvailabilityStatus.PENDING})

    # --- início manual ---

    def test_manual_start_blocked_until_evaluating_members_confirm(self) -> None:
        self._respond(self.president_user, self.president, AvailabilityStatus.CONFIRMED)
        with self.assertRaises(ValidationError) as context:
            start_defense(self.defense, actor=self.admin)
        self.assertIn("Prof. Membro", context.exception.messages[0])
        self.assertNotIn("Profa. Presidente", context.exception.messages[0])
        self.assertNotIn("Prof. Suplente", context.exception.messages[0])
        self.assertEqual(Defense.objects.get(pk=self.defense.pk).status, DefenseStatus.SCHEDULED)

    def test_manual_start_api_returns_400_with_missing_names(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(f"/api/v1/defenses/{self.defense.pk}/start", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Confirmação de disponibilidade pendente: Profa. Presidente, Prof. Membro.")

    def test_declined_member_blocks_start(self) -> None:
        confirm_committee(self.defense)
        DefenseMember.objects.filter(pk=self.member.pk).update(availability_status=AvailabilityStatus.DECLINED)
        with self.assertRaises(ValidationError):
            start_defense(self.defense, actor=self.admin)

    def test_alternate_pending_does_not_block_start(self) -> None:
        self._respond(self.president_user, self.president, AvailabilityStatus.CONFIRMED)
        self._respond(self.member_user, self.member, AvailabilityStatus.CONFIRMED)
        start_defense(self.defense, actor=self.admin)
        self.assertEqual(Defense.objects.get(pk=self.defense.pk).status, DefenseStatus.IN_PROGRESS)

    def test_defense_without_evaluating_members_keeps_manual_start(self) -> None:
        DefenseMember.objects.filter(defense=self.defense).exclude(role=CommitteeRole.ALTERNATE).delete()
        start_defense(self.defense, actor=self.admin)
        self.assertEqual(Defense.objects.get(pk=self.defense.pk).status, DefenseStatus.IN_PROGRESS)

    # --- abertura automática e detalhe ---

    def test_auto_start_blocked_without_confirmations(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=5))
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.status_code, 200)
        detail = response.data["defense"]
        self.assertEqual(detail["status"], DefenseStatus.SCHEDULED)
        self.assertFalse(detail["allMembersConfirmed"])
        self.assertEqual([item["name"] for item in detail["pendingConfirmations"]], ["Profa. Presidente", "Prof. Membro"])
        self.assertEqual(detail["pendingConfirmations"][0]["memberId"], str(self.president.pk))
        listing = self.client.get("/api/v1/defenses", {"status": "SCHEDULED"})
        self.assertEqual(listing.data["count"], 1)

    def test_auto_start_runs_after_last_confirmation(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self._respond(self.president_user, self.president, AvailabilityStatus.CONFIRMED).status_code, 200)
        self.assertEqual(self._respond(self.member_user, self.member, AvailabilityStatus.CONFIRMED).status_code, 200)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)
        self.assertTrue(response.data["defense"]["allMembersConfirmed"])
        self.assertEqual(response.data["defense"]["pendingConfirmations"], [])

    def test_detail_without_members_reports_not_all_confirmed(self) -> None:
        DefenseMember.objects.filter(defense=self.defense).delete()
        self.client.force_authenticate(user=self.admin)
        detail = self.client.get(f"/api/v1/defenses/{self.defense.pk}").data["defense"]
        self.assertFalse(detail["allMembersConfirmed"])
        self.assertEqual(detail["pendingConfirmations"], [])


@override_settings(CACHES=TEST_CACHES)
class AvailabilityMigrationDataTests(TestCase):
    """Migration 0006 marks members of already started or completed defenses as CONFIRMED."""

    def test_started_and_completed_defense_members_become_confirmed(self) -> None:
        department = Department.objects.create(name="Computação")
        coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=department, coordinator=coordinator, name="CC", code="CC001")
        admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        members_by_status: dict[str, DefenseMember] = {}
        for index, defense_status in enumerate((DefenseStatus.SCHEDULED, DefenseStatus.IN_PROGRESS, DefenseStatus.COMPLETED, DefenseStatus.CANCELLED)):
            thesis = Thesis.objects.create(course=course, advisor=advisor, title=f"Tese {index}", status=ThesisStatus.ADVISOR_APPROVED)
            defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=index + 1), actor=admin)
            member_user = create_user(f"m{index}@ufvjm.edu.br", f"Membro {index}", ProfileRole.ADVISOR)
            members_by_status[defense_status] = add_institutional_member(defense, CommitteeRole.MEMBER, member_user, actor=admin)
            Defense.objects.filter(pk=defense.pk).update(status=defense_status)

        migration_module = importlib.import_module("theses.migrations.0006_defense_member_availability")
        migration_module.mark_started_defense_members_confirmed(django_apps, None)

        expected = {
            DefenseStatus.SCHEDULED: AvailabilityStatus.PENDING,
            DefenseStatus.IN_PROGRESS: AvailabilityStatus.CONFIRMED,
            DefenseStatus.COMPLETED: AvailabilityStatus.CONFIRMED,
            DefenseStatus.CANCELLED: AvailabilityStatus.PENDING,
        }
        for defense_status, member in members_by_status.items():
            member.refresh_from_db()
            self.assertEqual(member.availability_status, expected[defense_status], defense_status)
