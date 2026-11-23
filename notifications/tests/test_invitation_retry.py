"""
Tests for ``--retry-pending`` on external member invitation e-mails: a new token every time.
"""

from __future__ import annotations

import io
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import AuditLog
from accounts.services import hash_external_token, revoke_external_invitation
from events.catalog import ExternalInvitationTrigger
from notifications.consumer import handle_event
from notifications.delivery import retry_pending_notifications
from notifications.invitations import MASKED_ACCESS_TOKEN
from notifications.models import Notification, NotificationStatus
from notifications.tests.invitation_fixtures import INVITATION_TEST_SETTINGS, InvitationFixtureMixin, envelope_for
from notifications.tests.token_leaks import assert_token_not_logged, assert_token_not_stored, capture_all_logs, last_emailed_token, tokens_in_outbox
from theses.models import Defense, DefenseStatus


@override_settings(**INVITATION_TEST_SETTINGS)
class InvitationRetryTests(InvitationFixtureMixin, TestCase):
    """
    Retrying an invitation e-mail issues a new token instead of resending the stored body.
    """

    def _deliver_with_failure(self) -> Notification:
        event = self._record()
        with patch("notifications.delivery.EmailMultiAlternatives.send", side_effect=RuntimeError("relay down")):
            handle_event(envelope_for(event))
        notification = Notification.objects.get()
        self.assertEqual(notification.status, NotificationStatus.FAILED)
        return notification

    def _age(self, notification: Notification, minutes: int = 10) -> None:
        Notification.objects.filter(pk=notification.pk).update(scheduled_at=timezone.now() - timedelta(minutes=minutes))

    def test_failed_invitation_is_resent_with_a_new_working_token(self) -> None:
        notification = self._deliver_with_failure()
        hash_after_failure = bytes(self._invitation().token_hash)
        with capture_all_logs(self) as captured:
            summary = retry_pending_notifications(limit=10)
        self.assertEqual((summary.sent, summary.failed, summary.cancelled), (1, 0, 0))
        token = last_emailed_token(self)
        invitation = self._invitation()
        self.assertNotEqual(bytes(invitation.token_hash), hash_after_failure)
        self.assertEqual(bytes(invitation.token_hash), hash_external_token(token))
        self.assertIsNotNone(invitation.token_sent_at)
        notification.refresh_from_db()
        self.assertEqual((notification.status, notification.attempts), (NotificationStatus.SENT, 2))
        self.assertIn(MASKED_ACCESS_TOKEN, notification.body)
        self.assertIn(MASKED_ACCESS_TOKEN, notification.body_html)
        audit = AuditLog.objects.get(metadata__event="INVITATION_EMAIL_SENT")
        self.assertEqual(audit.metadata["attempts"], 2)
        assert_token_not_stored(self, token)
        assert_token_not_logged(self, captured, token)
        self.assertEqual(self._exchange_status(token), 200)

    def test_retry_invalidates_the_token_of_the_previous_email(self) -> None:
        event = self._record()
        handle_event(envelope_for(event))
        first_token = last_emailed_token(self)
        notification = Notification.objects.get()
        Notification.objects.filter(pk=notification.pk).update(status=NotificationStatus.FAILED)
        with capture_all_logs(self) as captured:
            retry_pending_notifications(limit=10)
        second_token = last_emailed_token(self)
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(self._exchange_status(first_token), 401)
        self.assertEqual(self._exchange_status(second_token), 200)
        for token in (first_token, second_token):
            assert_token_not_stored(self, token)
            assert_token_not_logged(self, captured, token)

    def test_stale_pending_invitation_gets_a_new_token_but_fresh_pending_is_left_alone(self) -> None:
        event = self._record()
        with patch("notifications.consumer.send_notification", return_value=False):
            handle_event(envelope_for(event))
        notification = Notification.objects.get()
        self.assertEqual(notification.status, NotificationStatus.PENDING)
        retry_pending_notifications(limit=10)
        self.assertEqual(mail.outbox, [])
        self._age(notification)
        retry_pending_notifications(limit=10)
        token = last_emailed_token(self)
        self.assertEqual(self._exchange_status(token), 200)

    def test_superseded_invitation_email_is_cancelled_without_new_token(self) -> None:
        stale = self._deliver_with_failure()
        newer = self._record(ExternalInvitationTrigger.RESENT)
        handle_event(envelope_for(newer))
        current_token = last_emailed_token(self)
        hash_before_retry = bytes(self._invitation().token_hash)
        mail.outbox.clear()
        output = io.StringIO()
        with capture_all_logs(self) as captured:
            call_command("consume_notifications", "--retry-pending", stdout=output)
        self.assertEqual(mail.outbox, [])
        stale.refresh_from_db()
        self.assertEqual(stale.status, NotificationStatus.CANCELLED)
        self.assertEqual(bytes(self._invitation().token_hash), hash_before_retry)
        self.assertIn("Convites cancelados (substituídos ou inválidos): 1.", output.getvalue())
        assert_token_not_logged(self, captured, current_token)
        self.assertEqual(self._exchange_status(current_token), 200)
        retry_pending_notifications(limit=10)
        stale.refresh_from_db()
        self.assertEqual(stale.status, NotificationStatus.CANCELLED)

    def test_revoked_invitation_email_is_cancelled(self) -> None:
        notification = self._deliver_with_failure()
        revoke_external_invitation(self._invitation(), revoked_by=self.admin)
        summary = retry_pending_notifications(limit=10)
        self.assertEqual(summary.cancelled, 1)
        self.assertEqual(mail.outbox, [])
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.CANCELLED)

    def test_cancelled_defense_invitation_email_is_cancelled(self) -> None:
        self._deliver_with_failure()
        Defense.objects.filter(pk=self.defense.pk).update(status=DefenseStatus.CANCELLED)
        summary = retry_pending_notifications(limit=10)
        self.assertEqual((summary.sent, summary.cancelled), (0, 1))

    @override_settings(EMAIL_BACKEND="notifications.tests.invitation_fixtures.EchoingFailingEmailBackend")
    def test_failing_again_keeps_the_token_out_of_the_row_and_logs(self) -> None:
        notification = self._deliver_with_failure()
        with capture_all_logs(self) as captured:
            summary = retry_pending_notifications(limit=10)
        self.assertEqual(summary.failed, 1)
        notification.refresh_from_db()
        self.assertEqual(notification.status, NotificationStatus.FAILED)
        self.assertIn("UFVJM-****", notification.error_message)
        self.assertIsNone(self._invitation().token_sent_at)
        for record in captured.records:
            self.assertNotRegex(record.getMessage(), r"UFVJM(?:-[A-Z0-9]{4}){4}")
        self.assertEqual(tokens_in_outbox(), [])
