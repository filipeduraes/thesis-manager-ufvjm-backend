"""
Tests for external committee member invitations e-mailed through the event queue,
session assignments and access scope.
"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import AuditLog, ExternalInvitation, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import hash_external_token
from events.catalog import EventType
from events.models import OutboxEvent
from notifications.tests.invitation_fixtures import deliver_pending_invitations
from notifications.tests.token_leaks import assert_envelopes_without_token, assert_token_not_logged, assert_token_not_stored, capture_all_logs, last_emailed_token
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import AvailabilityStatus, CommitteeRole, DefenseMember, DefenseMemberEvaluation, DefenseResult, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", FRONTEND_URL="https://tcc.example.com")
class ExternalMemberTokenTests(TestCase):
    """The token is e-mailed by the consumer, never returned by the API, persisted in clear or logged."""

    def setUp(self) -> None:
        # Os limites de requisição ficam no cache; sem limpar, um teste herda o consumo do anterior.
        cache.clear()
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

    def _deliver(self) -> str:
        mail.outbox.clear()
        deliver_pending_invitations()
        return last_emailed_token(self)

    def _add_and_deliver(self) -> tuple[str, str]:
        member_id = self._add_external().data["member"]["id"]
        return member_id, self._deliver()

    def _regenerate(self, member_id: str) -> dict:
        return self.manager_client.post(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/regenerate-token", {}, format="json")

    def _exchange_status(self, token: str) -> int:
        return APIClient().post("/api/v1/auth/external-token", {"token": token}, format="json").status_code

    def _external_session(self, token: str) -> APIClient:
        client = APIClient()
        response = client.post("/api/v1/auth/external-token", {"token": token}, format="json")
        self.assertEqual(response.status_code, 200)
        return client

    # --- token só no e-mail ---

    def test_add_external_never_returns_token_and_emails_a_working_one(self) -> None:
        with capture_all_logs(self) as captured:
            response = self._add_external()
            self.assertNotIn("accessToken", response.data)
            self.assertNotIn("accessTokenExpiresAt", response.data)
            member_payload = response.data["member"]
            self.assertEqual(member_payload["invitationStatus"], InvitationStatus.PENDING)
            self.assertIsNone(member_payload["invitationSentAt"])
            self.assertTrue(member_payload["canResendInvitation"])
            [event] = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED)
            self.assertEqual(event.payload, {"defenseId": str(self.defense.pk), "thesisId": str(self.thesis.pk), "memberId": member_payload["id"], "invitationId": str(DefenseMember.objects.get(pk=member_payload["id"]).external_invitation_id), "trigger": "CREATED"})
            mail.outbox.clear()
            envelopes = deliver_pending_invitations()
            token = last_emailed_token(self)
        self.assertEqual(mail.outbox[-1].to, ["externa@example.com"])
        member = DefenseMember.objects.select_related("external_invitation").get(pk=member_payload["id"])
        self.assertEqual(bytes(member.external_invitation.token_hash), hash_external_token(token))
        assert_envelopes_without_token(self, envelopes, token)
        assert_token_not_stored(self, token)
        assert_token_not_logged(self, captured, token)
        detail = self.manager_client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertNotIn(token, str(detail.data))
        detail_member = detail.data["defense"]["members"][0]
        self.assertNotIn("accessToken", detail_member)
        self.assertIsNotNone(detail_member["invitationSentAt"])
        self._external_session(token)

    def test_add_external_payload_keeps_member_fields(self) -> None:
        member_payload = self._add_external().data["member"]
        for key in ("id", "role", "roleLabel", "name", "email", "institution", "isInstitutional", "availabilityStatus", "invitationStatus", "invitationStatusLabel", "invitationExpiresAt", "invitationSentAt", "canResendInvitation"):
            self.assertIn(key, member_payload)
        self.assertFalse(member_payload["isInstitutional"])

    def test_invitation_state_is_hidden_from_viewers_who_do_not_manage(self) -> None:
        member_id, token = self._add_and_deliver()
        client = self._external_session(token)
        detail = client.get(f"/api/v1/defenses/{self.defense.pk}").data
        [member_payload] = [member for member in detail["defense"]["members"] if member["id"] == member_id]
        self.assertIsNone(member_payload["invitationStatus"])
        self.assertIsNone(member_payload["invitationSentAt"])
        self.assertFalse(member_payload["canResendInvitation"])

    # --- novo convite ---

    def test_regenerate_revokes_old_invitation_and_emails_new_token(self) -> None:
        member_id, old_token = self._add_and_deliver()
        old_invitation_id = DefenseMember.objects.get(pk=member_id).external_invitation_id
        with capture_all_logs(self) as captured:
            response = self._regenerate(member_id)
            self.assertEqual(response.status_code, 202)
            self.assertNotIn("accessToken", response.data)
            self.assertEqual(response.data["member"]["id"], member_id)
            self.assertIsNone(response.data["member"]["invitationSentAt"])
            new_token = self._deliver()
        self.assertNotEqual(new_token, old_token)
        old_invitation = ExternalInvitation.objects.get(pk=old_invitation_id)
        self.assertEqual(old_invitation.status, InvitationStatus.REVOKED)
        self.assertEqual(self._exchange_status(old_token), 401)
        self._external_session(new_token)
        regenerated_event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED).latest("created_at")
        self.assertEqual(regenerated_event.payload["trigger"], "REGENERATED")
        self.assertTrue(AuditLog.objects.filter(metadata__event="EXTERNAL_TOKEN_REGENERATED").exists())
        for token in (old_token, new_token):
            assert_token_not_stored(self, token)
            assert_token_not_logged(self, captured, token)

    def test_regenerate_keeps_member_availability_and_evaluation(self) -> None:
        member_id, token = self._add_and_deliver()
        external_client = self._external_session(token)
        confirm = external_client.post(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/availability", {"status": AvailabilityStatus.CONFIRMED, "note": "Disponível"}, format="json")
        self.assertEqual(confirm.status_code, 200)
        start_defense(self.defense, actor=self.admin)
        evaluation = external_client.put(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/evaluation", {"decision": DefenseResult.APPROVED_WITH_REMARKS, "score": "8.50", "notes": "Ajustar referências"}, format="json")
        self.assertEqual(evaluation.status_code, 200)
        response = self._regenerate(member_id)
        self.assertEqual(response.status_code, 202)
        member = DefenseMember.objects.get(pk=member_id)
        self.assertEqual(member.availability_status, AvailabilityStatus.CONFIRMED)
        self.assertEqual(member.availability_note, "Disponível")
        self.assertIsNotNone(member.availability_responded_at)
        saved = DefenseMemberEvaluation.objects.get(member=member)
        self.assertEqual((saved.decision, str(saved.score), saved.notes), (DefenseResult.APPROVED_WITH_REMARKS, "8.50", "Ajustar referências"))
        new_client = self._external_session(self._deliver())
        own_evaluation = new_client.get(f"/api/v1/defenses/{self.defense.pk}/members/{member_id}/evaluation")
        self.assertEqual(own_evaluation.status_code, 200)
        self.assertEqual(own_evaluation.data["evaluation"]["decision"], DefenseResult.APPROVED_WITH_REMARKS)

    def test_regenerate_works_for_used_invitation(self) -> None:
        member_id, token = self._add_and_deliver()
        self._external_session(token)
        member_payload = self.manager_client.get(f"/api/v1/defenses/{self.defense.pk}").data["defense"]["members"][0]
        self.assertEqual(member_payload["invitationStatus"], InvitationStatus.USED)
        self.assertFalse(member_payload["canResendInvitation"])
        self.assertEqual(self._regenerate(member_id).status_code, 202)
        self._external_session(self._deliver())

    def test_regenerate_works_for_expired_invitation(self) -> None:
        member_id, token = self._add_and_deliver()
        expired_invitation_id = DefenseMember.objects.get(pk=member_id).external_invitation_id
        ExternalInvitation.objects.filter(pk=expired_invitation_id).update(expires_at=timezone.now() - timedelta(minutes=1))
        member_payload = self.manager_client.get(f"/api/v1/defenses/{self.defense.pk}").data["defense"]["members"][0]
        self.assertEqual(member_payload["invitationStatus"], InvitationStatus.EXPIRED)
        self.assertFalse(member_payload["canResendInvitation"])
        self.assertEqual(self._exchange_status(token), 401)
        with capture_all_logs(self) as captured:
            response = self._regenerate(member_id)
            new_token = self._deliver()
        self.assertEqual(response.status_code, 202)
        new_invitation = DefenseMember.objects.select_related("external_invitation").get(pk=member_id).external_invitation
        self.assertNotEqual(new_invitation.pk, expired_invitation_id)
        self.assertGreater(new_invitation.expires_at, timezone.now())
        self._external_session(new_token)
        assert_token_not_stored(self, new_token)
        assert_token_not_logged(self, captured, new_token)

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
        regenerate = self._regenerate(member_id)
        self.assertEqual(regenerate.status_code, 400)

    # --- sessão externa ---

    def test_session_lists_assignments_without_changing_existing_fields(self) -> None:
        member_id, token = self._add_and_deliver()
        client = self._external_session(token)
        session = client.get("/api/v1/session").data
        invitation = session["externalInvitation"]
        for key in ("id", "name", "email", "institution", "expiresAt"):
            self.assertIn(key, invitation)
        self.assertEqual(invitation["assignments"], [{"defenseId": str(self.defense.pk), "memberId": member_id}])
        self.assertEqual(session["authType"], "external")

    def test_institutional_session_has_no_external_assignments(self) -> None:
        session = self.manager_client.get("/api/v1/session").data
        self.assertIsNone(session["externalInvitation"])

    def test_external_session_scope(self) -> None:
        member_id, token = self._add_and_deliver()
        client = self._external_session(token)
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
