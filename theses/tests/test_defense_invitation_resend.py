"""
Tests for ``POST /api/v1/defenses/<id>/members/<memberId>/resend-invitation``.
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
from accounts.services import revoke_external_invitation
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent
from notifications.consumer import HandleOutcome, handle_event
from notifications.tests.invitation_fixtures import deliver_pending_invitations
from notifications.tests.token_leaks import assert_envelopes_without_token, assert_token_not_logged, assert_token_not_stored, capture_all_logs, last_emailed_token
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", FRONTEND_URL="https://tcc.example.com")
class ResendInvitationTests(TestCase):
    """
    Only defense managers resend, the old token dies at once and the new one exists only in the e-mail.
    """

    def setUp(self) -> None:
        # Os limites de requisição ficam no cache; sem limpar, um teste herda o consumo do anterior.
        cache.clear()
        department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.other_coordinator = create_user("coord2@ufvjm.edu.br", "Coord Outro Curso", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=department, coordinator=self.coordinator, name="CC", code="CC001")
        Course.objects.create(department=department, coordinator=self.other_coordinator, name="SI", code="SI001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.committee_advisor = create_user("banca@ufvjm.edu.br", "Membro Interno", ProfileRole.ADVISOR)
        self.student = create_user("aluno@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        self.thesis = Thesis.objects.create(course_id=course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        add_institutional_member(self.defense, CommitteeRole.MEMBER, self.committee_advisor, actor=self.admin)
        self.advisor_client = self._client_for(self.advisor)
        payload = {"role": CommitteeRole.MEMBER, "name": "Dra. Externa", "email": "externa@example.com", "institution": "UFMG"}
        response = self.advisor_client.post(f"/api/v1/defenses/{self.defense.pk}/members/external", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.member_id = response.data["member"]["id"]
        self.first_token = self._deliver()

    def _client_for(self, user: User) -> APIClient:
        client = APIClient()
        client.force_authenticate(user=user)
        return client

    def _url(self, member_id: str | None = None) -> str:
        return f"/api/v1/defenses/{self.defense.pk}/members/{member_id or self.member_id}/resend-invitation"

    def _deliver(self) -> str:
        mail.outbox.clear()
        deliver_pending_invitations()
        return last_emailed_token(self)

    def _exchange_status(self, token: str) -> int:
        return APIClient().post("/api/v1/auth/external-token", {"token": token}, format="json").status_code

    def _invitation(self) -> ExternalInvitation:
        member = DefenseMember.objects.only("external_invitation_id").get(pk=self.member_id)
        return ExternalInvitation.objects.filter(pk=member.external_invitation_id).first()

    def _invited_events(self) -> int:
        return OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED).count()

    # --- fluxo principal ---

    def test_resend_invalidates_old_token_at_once_and_emails_a_new_one(self) -> None:
        expires_before = self._invitation().expires_at
        with capture_all_logs(self) as captured:
            response = self.advisor_client.post(self._url(), {}, format="json")
            self.assertEqual(response.status_code, 202)
            self.assertNotIn("accessToken", response.data)
            self.assertIsNone(response.data["member"]["invitationSentAt"])
            self.assertTrue(response.data["member"]["canResendInvitation"])
            self.assertEqual(self._exchange_status(self.first_token), 401)
            mail.outbox.clear()
            envelopes = deliver_pending_invitations()
            new_token = last_emailed_token(self)
        self.assertNotEqual(new_token, self.first_token)
        self.assertEqual(mail.outbox[-1].to, ["externa@example.com"])
        invitation = self._invitation()
        self.assertEqual(invitation.expires_at, expires_before)
        self.assertIsNotNone(invitation.token_sent_at)
        resent_event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED).latest("created_at")
        self.assertEqual(resent_event.payload["trigger"], "RESENT")
        request_audit = AuditLog.objects.get(metadata__event="INVITATION_RESEND_REQUESTED")
        self.assertEqual(request_audit.actor_user_id, self.advisor.pk)
        self.assertEqual(request_audit.metadata["eventId"], str(resent_event.pk))
        self.assertTrue(AuditLog.objects.filter(metadata__event="INVITATION_EMAIL_RESENT", metadata__eventId=str(resent_event.pk)).exists())
        for token in (self.first_token, new_token):
            assert_envelopes_without_token(self, envelopes, token)
            assert_token_not_stored(self, token)
            assert_token_not_logged(self, captured, token)
        self.assertEqual(self._exchange_status(new_token), 200)

    def test_redelivered_resend_event_issues_no_new_token_nor_email(self) -> None:
        self.advisor_client.post(self._url(), {}, format="json")
        new_token = self._deliver()
        resent_event = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EXTERNAL_MEMBER_INVITED).latest("created_at")
        hash_before = bytes(self._invitation().token_hash)
        mail.outbox.clear()
        envelope = EventEnvelope.from_json_bytes(EventEnvelope.from_outbox(resent_event).to_json_bytes())
        self.assertEqual(handle_event(envelope), HandleOutcome.DUPLICATE)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(bytes(self._invitation().token_hash), hash_before)
        self.assertEqual(self._exchange_status(new_token), 200)

    # --- permissões ---

    def test_every_manager_role_can_resend(self) -> None:
        emailed_tokens = [self.first_token]
        for user in (self.admin, self.coordinator, self.advisor):
            with self.subTest(user=user.email):
                response = self._client_for(user).post(self._url(), {}, format="json")
                self.assertEqual(response.status_code, 202)
                emailed_tokens.append(self._deliver())
        self.assertEqual(len(set(emailed_tokens)), 4)
        for stale_token in emailed_tokens[:-1]:
            self.assertEqual(self._exchange_status(stale_token), 401)
        self.assertEqual(self._exchange_status(emailed_tokens[-1]), 200)

    def test_users_who_do_not_manage_the_defense_cannot_resend(self) -> None:
        events_before = self._invited_events()
        expected = {self.other_advisor: 404, self.other_coordinator: 404, self.student: 404, self.committee_advisor: 403}
        for user, status_code in expected.items():
            with self.subTest(user=user.email):
                self.assertEqual(self._client_for(user).post(self._url(), {}, format="json").status_code, status_code)
        self.assertIn(APIClient().post(self._url(), {}, format="json").status_code, {401, 403})
        self.assertEqual(self._invited_events(), events_before)
        self.assertEqual(self._exchange_status(self.first_token), 200)

    def test_external_session_cannot_resend(self) -> None:
        external_client = APIClient()
        self.assertEqual(external_client.post("/api/v1/auth/external-token", {"token": self.first_token}, format="json").status_code, 200)
        events_before = self._invited_events()
        self.assertEqual(external_client.post(self._url(), {}, format="json").status_code, 403)
        self.assertEqual(self._invited_events(), events_before)

    # --- estados que impedem o reenvio ---

    def _assert_refused(self, expected_message: str) -> None:
        events_before = self._invited_events()
        hash_before = bytes(self._invitation().token_hash)
        response = self.advisor_client.post(self._url(), {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn(expected_message, str(response.data))
        self.assertEqual(self._invited_events(), events_before)
        self.assertEqual(bytes(self._invitation().token_hash), hash_before)

    def test_revoked_invitation_cannot_be_resent(self) -> None:
        revoke_external_invitation(self._invitation(), revoked_by=self.admin)
        self._assert_refused("Convite revogado não pode ser reenviado")

    def test_used_invitation_cannot_be_resent(self) -> None:
        self.assertEqual(self._exchange_status(self.first_token), 200)
        self.assertEqual(self._invitation().status, InvitationStatus.USED)
        self._assert_refused("Convite já utilizado não pode ser reenviado")

    def test_expired_invitation_cannot_be_resent(self) -> None:
        ExternalInvitation.objects.filter(pk=self._invitation().pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self._assert_refused("Convite expirado não pode ser reenviado")

    def test_inactive_defense_cannot_resend(self) -> None:
        for defense_status in (DefenseStatus.CANCELLED, DefenseStatus.COMPLETED):
            with self.subTest(status=defense_status):
                Defense.objects.filter(pk=self.defense.pk).update(status=defense_status)
                self._assert_refused("Só é possível reenviar convites de bancas agendadas ou em andamento.")

    def test_institutional_member_has_no_invitation_to_resend(self) -> None:
        institutional = DefenseMember.objects.get(defense=self.defense, institutional_member=self.committee_advisor)
        response = self.advisor_client.post(self._url(str(institutional.pk)), {}, format="json")
        self.assertEqual(response.status_code, 400)

    # --- limite de requisições ---

    def test_resend_is_rate_limited_per_user_together_with_new_invitations(self) -> None:
        for _attempt in range(5):
            self.assertEqual(self.advisor_client.post(self._url(), {}, format="json").status_code, 202)
        events_before = self._invited_events()
        self.assertEqual(self.advisor_client.post(self._url(), {}, format="json").status_code, 429)
        regenerate_url = f"/api/v1/defenses/{self.defense.pk}/members/{self.member_id}/regenerate-token"
        self.assertEqual(self.advisor_client.post(regenerate_url, {}, format="json").status_code, 429)
        self.assertEqual(self._invited_events(), events_before)
        self.assertEqual(self._client_for(self.admin).post(self._url(), {}, format="json").status_code, 202)
