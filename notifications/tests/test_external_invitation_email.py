"""
Tests for the e-mail delivery of external committee member tokens through the event queue.
"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AuditAction, AuditLog, ExternalInvitation
from accounts.services import hash_external_token, revoke_external_invitation
from events.catalog import ExternalInvitationTrigger
from events.envelope import EventEnvelope
from events.publisher import publish_pending_events
from events.tests.fakes import InMemoryEventBroker
from notifications.consumer import HandleOutcome, handle_event
from notifications.invitations import MASKED_ACCESS_TOKEN
from notifications.models import Notification, NotificationStatus, NotificationTemplateCode
from notifications.tests.invitation_fixtures import INVITATION_TEST_SETTINGS, InvitationFixtureMixin, envelope_for
from notifications.tests.token_leaks import assert_envelopes_without_token, assert_token_not_logged, assert_token_not_stored, capture_all_logs, last_emailed_token, tokens_in_outbox
from theses.models import Defense, DefenseMember, DefenseStatus


@override_settings(**INVITATION_TEST_SETTINGS)
class ExternalInvitationEmailTests(InvitationFixtureMixin, TestCase):
    """
    The consumer issues the token, e-mails it and keeps only its hash.
    """

    def test_event_payload_has_only_identifiers(self) -> None:
        event = self._record()
        self.assertEqual(set(event.payload), {"defenseId", "thesisId", "memberId", "invitationId", "trigger"})
        self.assertEqual(event.payload["trigger"], "CREATED")
        self.assertNotIn("externo@example.org", str(event.payload))
        self.assertEqual(self._invitation().token_event_id, event.pk)

    def test_consumer_emails_working_token_and_never_stores_or_logs_it(self) -> None:
        self._record()
        broker = InMemoryEventBroker()
        with capture_all_logs(self) as captured:
            publish_pending_events(broker, batch_size=10)
            [envelope] = [published for published in broker.published if published.event_type == "defense.external_member_invited"]
            outcome = handle_event(EventEnvelope.from_json_bytes(envelope.to_json_bytes()))
        self.assertEqual(outcome, HandleOutcome.PROCESSED)
        [message] = mail.outbox
        self.assertEqual(message.to, ["externo@example.org"])
        token = last_emailed_token(self)
        self.assertIn("membro", message.body)
        self.assertIn("https://tcc.example.com/login", message.body)
        self.assertNotIn("meet.example.com", message.body)
        invitation = self._invitation()
        self.assertEqual(bytes(invitation.token_hash), hash_external_token(token))
        self.assertIsNotNone(invitation.token_sent_at)
        notification = Notification.objects.get(external_invitation=invitation)
        self.assertEqual(notification.status, NotificationStatus.SENT)
        self.assertEqual(notification.template.code, NotificationTemplateCode.EXTERNAL_MEMBER_INVITED)
        self.assertIn(MASKED_ACCESS_TOKEN, notification.body)
        self.assertIn(MASKED_ACCESS_TOKEN, notification.body_html)
        assert_envelopes_without_token(self, broker.published, token)
        assert_token_not_stored(self, token)
        captured_loggers = {record.name for record in captured.records}
        self.assertIn("notifications.consumer", captured_loggers)
        self.assertIn("events.publisher", captured_loggers)
        assert_token_not_logged(self, captured, token)
        self.assertNotIn("externo@example.org", "\n".join(captured.output))
        self.assertEqual(self._exchange_status(token), 200)

    def test_delivery_is_audited_with_identifiers_only(self) -> None:
        event = self._record()
        handle_event(envelope_for(event))
        token = last_emailed_token(self)
        entry = AuditLog.objects.filter(action=AuditAction.INVITE, metadata__event="INVITATION_EMAIL_SENT").get()
        self.assertEqual(entry.entity_id, str(self.member.external_invitation_id))
        self.assertIsNone(entry.actor_user_id)
        self.assertEqual(entry.metadata["eventId"], str(event.pk))
        self.assertEqual(entry.metadata["memberId"], str(self.member.pk))
        self.assertEqual(entry.metadata["defenseId"], str(self.defense.pk))
        self.assertNotIn(token, str(entry.metadata))
        self.assertNotIn("externo@example.org", str(entry.metadata))

    def test_resent_delivery_is_audited_as_resend(self) -> None:
        handle_event(envelope_for(self._record()))
        handle_event(envelope_for(self._record(ExternalInvitationTrigger.RESENT)))
        self.assertTrue(AuditLog.objects.filter(metadata__event="INVITATION_EMAIL_RESENT", metadata__trigger="RESENT").exists())

    def test_redelivery_of_the_same_event_issues_no_new_token_nor_email(self) -> None:
        event = self._record()
        handle_event(envelope_for(event))
        token = last_emailed_token(self)
        hash_after_first = bytes(self._invitation().token_hash)
        with capture_all_logs(self) as captured:
            outcome = handle_event(envelope_for(event))
        self.assertEqual(outcome, HandleOutcome.DUPLICATE)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(bytes(self._invitation().token_hash), hash_after_first)
        self.assertEqual(Notification.objects.count(), 1)
        assert_token_not_logged(self, captured, token)
        self.assertEqual(self._exchange_status(token), 200)

    def test_superseded_event_issues_nothing_even_out_of_order(self) -> None:
        older = self._record()
        newer = self._record(ExternalInvitationTrigger.RESENT)
        handle_event(envelope_for(newer))
        token = last_emailed_token(self)
        hash_after_newer = bytes(self._invitation().token_hash)
        with capture_all_logs(self) as captured:
            outcome = handle_event(envelope_for(older))
        self.assertEqual(outcome, HandleOutcome.PROCESSED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(bytes(self._invitation().token_hash), hash_after_newer)
        assert_token_not_logged(self, captured, token)
        self.assertEqual(self._exchange_status(token), 200)

    def test_recording_a_new_event_invalidates_the_emailed_token(self) -> None:
        handle_event(envelope_for(self._record()))
        first_token = last_emailed_token(self)
        newer = self._record(ExternalInvitationTrigger.RESENT)
        self.assertIsNone(self._invitation().token_sent_at)
        self.assertEqual(self._exchange_status(first_token), 401)
        handle_event(envelope_for(newer))
        second_token = last_emailed_token(self)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(self._exchange_status(second_token), 200)
        assert_token_not_stored(self, first_token)
        assert_token_not_stored(self, second_token)

    def test_revoked_invitation_receives_nothing(self) -> None:
        event = self._record()
        revoke_external_invitation(self._invitation(), revoked_by=self.admin)
        handle_event(envelope_for(event))
        self.assertEqual(mail.outbox, [])
        self.assertFalse(Notification.objects.exists())

    def test_expired_invitation_receives_nothing(self) -> None:
        event = self._record()
        ExternalInvitation.objects.filter(pk=self.member.external_invitation_id).update(expires_at=timezone.now() - timedelta(minutes=1))
        handle_event(envelope_for(event))
        self.assertEqual(mail.outbox, [])

    def test_inactive_defense_receives_nothing(self) -> None:
        event = self._record()
        Defense.objects.filter(pk=self.defense.pk).update(status=DefenseStatus.CANCELLED)
        handle_event(envelope_for(event))
        self.assertEqual(mail.outbox, [])

    def test_removed_member_receives_nothing(self) -> None:
        event = self._record()
        DefenseMember.objects.filter(pk=self.member.pk).delete()
        self.assertEqual(handle_event(envelope_for(event)), HandleOutcome.PROCESSED)
        self.assertEqual(mail.outbox, [])

    @override_settings(EMAIL_BACKEND="notifications.tests.invitation_fixtures.EchoingFailingEmailBackend")
    def test_failed_delivery_keeps_token_out_of_the_error_and_logs(self) -> None:
        event = self._record()
        with capture_all_logs(self) as captured:
            handle_event(envelope_for(event))
        notification = Notification.objects.get()
        self.assertEqual(notification.status, NotificationStatus.FAILED)
        self.assertIn("UFVJM-****", notification.error_message)
        self.assertIsNone(self._invitation().token_sent_at)
        self.assertFalse(AuditLog.objects.filter(metadata__event="INVITATION_EMAIL_SENT").exists())
        for record in captured.records:
            self.assertNotRegex(record.getMessage(), r"UFVJM(?:-[A-Z0-9]{4}){4}")
        self.assertEqual(tokens_in_outbox(), [])
