"""
Tests for external committee member tokens, session assignments and access scope.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import AuditLog, ExternalInvitation, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import hash_external_token
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import AvailabilityStatus, CommitteeRole, DefenseMember, DefenseResult, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def audit_contains(text: str) -> bool:
    for entry in AuditLog.objects.all():
        serialized = " ".join(str(value) for value in (entry.metadata, entry.changes, entry.entity_id, entry.user_agent))
        if text in serialized:
            return True
    return False


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ExternalMemberTokenTests(TestCase):
    """The readable token is returned once, regenerated on demand and never persisted or logged."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.other_thesis = Thesis.objects.create(course=self.course, advisor=self.other_advisor, title="Outra", status=ThesisStatus.ADVISOR_APPROVED)
        self.other_defense = schedule_defense(self.other_thesis, scheduled_datetime=timezone.now() + timedelta(days=8), actor=self.admin)
        self.manager_client = APIClient()
        self.manager_client.force_authenticate(user=self.advisor)

    def _add_external(self) -> dict:
        payload = {"role": CommitteeRole.MEMBER, "name": "Dra. Externa", "email": "externa@example.com", "institution": "UFMG"}
        response = self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members/external", payload, format="json")
        self.assertEqual(response.status_code, 201)
        return response

    def _external_session(self, token: str) -> APIClient:
        client = APIClient()
        response = client.post("/api/v1/auth/external-token", {"token": token}, format="json")
        self.assertEqual(response.status_code, 200)
        return client

    # --- token devolvido uma vez ---

    def test_add_external_returns_token_once_and_stores_only_hash(self) -> None:
        with self.assertLogs(level=logging.DEBUG) as captured:
            response = self._add_external()
        token = response.data["accessToken"]
        self.assertRegex(token, r"^UFVJM-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertIn("accessTokenExpiresAt", response.data)
        self.assertEqual(response["Cache-Control"], "no-store")
        member = DefenseMember.objects.select_related("external_invitation").get(pk=response.data["member"]["id"])
        self.assertEqual(bytes(member.external_invitation.token_hash), hash_external_token(token))
        self.assertFalse(audit_contains(token))
        self.assertNotIn(token, "\n".join(captured.output))
        detail = self.manager_client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertNotIn(token, str(detail.data))
        self.assertNotIn("accessToken", detail.data["defense"]["members"][0])

    def test_add_external_payload_keeps_member_fields(self) -> None:
        member_payload = self._add_external().data["member"]
        for key in ("id", "role", "roleLabel", "name", "email", "institution", "isInstitutional", "availabilityStatus"):
            self.assertIn(key, member_payload)
        self.assertFalse(member_payload["isInstitutional"])

    # --- regeneração ---

    def test_regenerate_revokes_old_token_and_returns_new_once(self) -> None:
        first = self._add_external()
        old_token = first.data["accessToken"]
        member_id = first.data["member"]["id"]
        old_invitation_id = DefenseMember.objects.get(pk=member_id).external_invitation_id
        response = self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/regenerate-token", {}, format="json")
        self.assertEqual(response.status_code, 200)
        new_token = response.data["accessToken"]
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(response.data["member"]["id"], member_id)
        self.assertEqual(response["Cache-Control"], "no-store")
        old_invitation = ExternalInvitation.objects.get(pk=old_invitation_id)
        self.assertEqual(old_invitation.status, InvitationStatus.REVOKED)
        self.assertEqual(APIClient().post("/api/v1/auth/external-token", {"token": old_token}, format="json").status_code, 401)
        self._external_session(new_token)
        self.assertFalse(audit_contains(old_token))
        self.assertFalse(audit_contains(new_token))
        self.assertTrue(AuditLog.objects.filter(metadata__event="EXTERNAL_TOKEN_REGENERATED").exists())

    def test_regenerate_keeps_member_answers(self) -> None:
        member_id = self._add_external().data["member"]["id"]
        DefenseMember.objects.filter(pk=member_id).update(availability_status=AvailabilityStatus.CONFIRMED)
        self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/regenerate-token", {}, format="json")
        self.assertEqual(DefenseMember.objects.get(pk=member_id).availability_status, AvailabilityStatus.CONFIRMED)

    def test_regenerate_requires_defense_management(self) -> None:
        member_id = self._add_external().data["member"]["id"]
        url = f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/regenerate-token"
        client = APIClient()
        client.force_authenticate(user=self.other_advisor)
        self.assertEqual(client.post(url, {}, format="json").status_code, 404)
        student = create_user("student@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        client.force_authenticate(user=student)
        self.assertEqual(client.post(url, {}, format="json").status_code, 404)

    def test_regenerate_rejects_institutional_member(self) -> None:
        response = self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members", {"role": CommitteeRole.MEMBER, "userId": str(self.other_advisor.pk)}, format="json")
        member_id = response.data["member"]["id"]
        regenerate = self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/regenerate-token", {}, format="json")
        self.assertEqual(regenerate.status_code, 400)

    # --- sessão externa ---

    def test_session_lists_assignments_without_changing_existing_fields(self) -> None:
        first = self._add_external()
        client = self._external_session(first.data["accessToken"])
        session = client.get("/api/v1/session").data
        invitation = session["externalInvitation"]
        for key in ("id", "name", "email", "institution", "expiresAt"):
            self.assertIn(key, invitation)
        self.assertEqual(invitation["assignments"], [{"defenseId": str(self.defense.pk), "memberId": first.data["member"]["id"]}])
        self.assertEqual(session["authType"], "external")

    def test_institutional_session_has_no_external_assignments(self) -> None:
        session = self.manager_client.get("/api/v1/session").data
        self.assertIsNone(session["externalInvitation"])

    def test_external_session_scope(self) -> None:
        first = self._add_external()
        member_id = first.data["member"]["id"]
        client = self._external_session(first.data["accessToken"])
        own = f"/api/v1/defenses/{self.defense.pk}"
        other = f"/api/v1/defenses/{self.other_defense.pk}"

        self.assertEqual(client.get(own).status_code, 200)
        self.assertEqual(client.post(f"{own}/members/{member_id}/availability", {"status": AvailabilityStatus.CONFIRMED}, format="json").status_code, 200)

        self.assertEqual(client.get(other).status_code, 403)
        self.assertEqual(client.post(f"{other}/members/{member_id}/availability", {"status": AvailabilityStatus.CONFIRMED}, format="json").status_code, 403)
        self.assertEqual(client.patch(own, {"notes": "x"}, format="json").status_code, 403)
        self.assertEqual(client.post(f"{own}/start", {}, format="json").status_code, 403)
        self.assertEqual(client.post(f"{own}/cancel", {}, format="json").status_code, 403)
        self.assertEqual(client.post(f"{own}/members/{member_id}/regenerate-token", {}, format="json").status_code, 403)
        self.assertEqual(client.get("/api/v1/defenses").status_code, 403)
        self.assertEqual(client.get("/api/v1/defenses/schedulable-theses").status_code, 403)
        self.assertEqual(client.get(f"/api/v1/theses/{self.thesis.pk}").status_code, 403)

        start_defense(self.defense, actor=self.admin)
        evaluation = client.put(f"{own}/members/{member_id}/evaluation", {"decision": DefenseResult.REJECTED}, format="json")
        self.assertEqual(evaluation.status_code, 200)
