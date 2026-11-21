"""
Tests for the manual resend of failed or stale notifications (``--retry-pending``).
"""

from __future__ import annotations

import io
import smtplib
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from accounts.models import User
from notifications.delivery import retry_pending_notifications
from notifications.models import Notification, NotificationStatus, NotificationTemplate, NotificationTemplateCode


class RetryPendingNotificationsTests(TestCase):
    """Only failed and stale pending e-mails are resent, each at most once per run."""

    def setUp(self) -> None:
        self.template = NotificationTemplate.objects.get(code=NotificationTemplateCode.THESIS_PUBLISHED)
        self.user = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Ana Aluna")

    def _notification(self, status: str, minutes_ago: int, attempts: int = 1) -> Notification:
        scheduled_at = timezone.now() - timedelta(minutes=minutes_ago)
        return Notification.objects.create(template=self.template, user=self.user, subject="Assunto", body="Corpo", body_html="<p>Corpo</p>", status=status, attempts=attempts, scheduled_at=scheduled_at)

    def test_resends_failed_and_stale_pending_only(self) -> None:
        failed = self._notification(NotificationStatus.FAILED, minutes_ago=30)
        stale_pending = self._notification(NotificationStatus.PENDING, minutes_ago=10, attempts=0)
        fresh_pending = self._notification(NotificationStatus.PENDING, minutes_ago=1, attempts=0)
        sent = self._notification(NotificationStatus.SENT, minutes_ago=30)
        with self.assertLogs("notifications", level="INFO"):
            summary = retry_pending_notifications(limit=10)
        self.assertEqual((summary.sent, summary.failed), (2, 0))
        self.assertEqual(len(mail.outbox), 2)
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts, failed.error_message), (NotificationStatus.SENT, 2, ""))
        stale_pending.refresh_from_db()
        self.assertEqual(stale_pending.status, NotificationStatus.SENT)
        fresh_pending.refresh_from_db()
        self.assertEqual(fresh_pending.status, NotificationStatus.PENDING)
        sent.refresh_from_db()
        self.assertEqual(sent.attempts, 1)

    def test_respects_limit_oldest_first(self) -> None:
        oldest = self._notification(NotificationStatus.FAILED, minutes_ago=60)
        newest = self._notification(NotificationStatus.FAILED, minutes_ago=10)
        with self.assertLogs("notifications", level="INFO"):
            retry_pending_notifications(limit=1)
        oldest.refresh_from_db()
        newest.refresh_from_db()
        self.assertEqual((oldest.status, newest.status), (NotificationStatus.SENT, NotificationStatus.FAILED))

    def test_failure_again_keeps_failed_and_counts_attempt(self) -> None:
        failed = self._notification(NotificationStatus.FAILED, minutes_ago=30)
        with patch("notifications.delivery.EmailMultiAlternatives.send", side_effect=smtplib.SMTPServerDisconnected("down")), self.assertLogs("notifications", level="INFO"):
            summary = retry_pending_notifications(limit=10)
        self.assertEqual((summary.sent, summary.failed), (0, 1))
        failed.refresh_from_db()
        self.assertEqual((failed.status, failed.attempts), (NotificationStatus.FAILED, 2))
        self.assertIn("SMTPServerDisconnected", failed.error_message)

    def test_command_reports_counters_and_validates_limit(self) -> None:
        self._notification(NotificationStatus.FAILED, minutes_ago=30)
        output = io.StringIO()
        with self.assertLogs("notifications", level="INFO"):
            call_command("consume_notifications", "--retry-pending", stdout=output)
        self.assertIn("Reenviadas: 1; falharam novamente: 0.", output.getvalue())
        with self.assertRaises(CommandError):
            call_command("consume_notifications", "--retry-pending", "--limit", "0")
