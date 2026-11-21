"""
Tests for the ``publish_outbox_events`` command and the publisher loop, using the in-memory broker.
"""

from __future__ import annotations

import io
import signal
import uuid
from unittest.mock import MagicMock, call, patch

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from events.models import OutboxEvent, OutboxStatus
from events.publisher import PublishSummary, run_publisher_loop
from events.tests.fakes import InMemoryEventBroker
from events.tests.test_publisher import create_event

BUILD_BROKER = "events.management.commands.publish_outbox_events.build_event_broker"


class PublishCommandTests(TestCase):
    """The command publishes a batch, reports outages and requeues failed events."""

    def setUp(self) -> None:
        self.broker = InMemoryEventBroker()
        patcher = patch(BUILD_BROKER, return_value=self.broker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_once_publishes_single_batch_and_closes_broker(self) -> None:
        event = create_event()
        output = io.StringIO()
        call_command("publish_outbox_events", "--once", stdout=output)
        event.refresh_from_db()
        self.assertEqual(event.status, OutboxStatus.COMPLETED)
        self.assertIn("Publicados: 1", output.getvalue())
        self.assertTrue(self.broker.closed)

    def test_once_with_unreachable_broker_fails_and_closes_broker(self) -> None:
        event = create_event()
        self.broker.unavailable = True
        with self.assertLogs("events.publisher", level="WARNING"), self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--once", stdout=io.StringIO())
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts), (OutboxStatus.PENDING, 0))
        self.assertTrue(self.broker.closed)

    def test_rejects_non_positive_batch_size(self) -> None:
        with self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--once", "--batch-size", "0", stdout=io.StringIO())

    def test_requeue_all_failed_events(self) -> None:
        failed = [create_event(), create_event()]
        published = create_event()
        OutboxEvent.objects.filter(pk__in=[event.pk for event in failed]).update(status=OutboxStatus.FAILED, attempts=10, last_error="EventBrokerError: unroutable")
        OutboxEvent.objects.filter(pk=published.pk).update(status=OutboxStatus.COMPLETED)
        output = io.StringIO()
        with self.assertLogs("events.publisher", level="INFO"):
            call_command("publish_outbox_events", "--requeue-failed", "all", stdout=output)
        self.assertIn("2 evento(s)", output.getvalue())
        for event in failed:
            event.refresh_from_db()
            self.assertEqual((event.status, event.attempts, event.failed_at), (OutboxStatus.PENDING, 0, None))
            self.assertEqual(event.last_error, "EventBrokerError: unroutable")
        published.refresh_from_db()
        self.assertEqual(published.status, OutboxStatus.COMPLETED)
        self.assertEqual(self.broker.published, [])

    def test_requeue_single_event_requires_failed_status(self) -> None:
        event = create_event()
        with self.assertLogs("events.publisher", level="INFO"), self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--requeue-failed", str(event.pk))
        with self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--requeue-failed", "nao-e-uuid")

    def test_loop_mode_installs_signal_handlers_that_stop_the_loop(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        stop_states: list[bool] = []

        def fake_loop(broker, stop_event, batch_size, poll_interval) -> None:
            handler = signal.getsignal(signal.SIGTERM)
            with self.assertLogs("events.management.commands.publish_outbox_events", level="INFO"):
                handler(signal.SIGTERM, None)
            stop_states.append(stop_event.is_set())

        with patch("events.management.commands.publish_outbox_events.run_publisher_loop", side_effect=fake_loop), self.assertLogs("events.management.commands.publish_outbox_events", level="INFO"):
            call_command("publish_outbox_events")
        self.assertEqual(stop_states, [True])
        self.assertTrue(self.broker.closed)


@override_settings(RABBITMQ_URL="")
class PublishCommandConfigurationTests(TestCase):
    """Without ``RABBITMQ_URL`` the command fails clearly instead of connecting somewhere."""

    def test_missing_broker_url_is_command_error(self) -> None:
        with self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--once")


class PublisherLoopTests(TestCase):
    """The loop chains full batches, backs off while the broker is down and stops on request."""

    def test_waits_follow_batch_outcomes(self) -> None:
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False, False, False, False, True]
        summaries = [PublishSummary(published=2), PublishSummary(broker_unavailable=True), PublishSummary(broker_unavailable=True), PublishSummary(published=1)]
        with patch("events.publisher.publish_pending_events", side_effect=summaries), self.assertLogs("events.publisher", level="INFO") as captured:
            run_publisher_loop(InMemoryEventBroker(), stop_event, batch_size=2, poll_interval=1.0)
        self.assertEqual(stop_event.wait.call_args_list, [call(2.0), call(4.0), call(1.0)])
        self.assertTrue(any("reachable again" in line for line in captured.output))

    def test_reconnect_wait_is_capped(self) -> None:
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False] * 8 + [True]
        summaries = [PublishSummary(broker_unavailable=True)] * 8
        with patch("events.publisher.publish_pending_events", side_effect=summaries):
            run_publisher_loop(InMemoryEventBroker(), stop_event, batch_size=10, poll_interval=2.0)
        waits = [wait_call.args[0] for wait_call in stop_event.wait.call_args_list]
        self.assertEqual(waits, [4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0])

    def test_stops_immediately_when_already_requested(self) -> None:
        stop_event = MagicMock()
        stop_event.is_set.return_value = True
        with patch("events.publisher.publish_pending_events") as publish:
            run_publisher_loop(InMemoryEventBroker(), stop_event, batch_size=10, poll_interval=2.0)
        publish.assert_not_called()

    def test_unknown_event_id_cannot_be_requeued(self) -> None:
        with patch(BUILD_BROKER), self.assertLogs("events.publisher", level="INFO"), self.assertRaises(CommandError):
            call_command("publish_outbox_events", "--requeue-failed", str(uuid.uuid4()))
