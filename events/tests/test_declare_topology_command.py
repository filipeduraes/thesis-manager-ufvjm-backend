"""
Tests for the ``declare_event_topology`` command (``pika.BlockingConnection`` mocked).
"""

from __future__ import annotations

import io
import socket
from unittest.mock import patch

import pika.exceptions
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, override_settings

BROKER_URL = "amqp://thesis_events:segredo@rabbitmq:5672/thesis_manager"
COMMAND_MODULE = "events.management.commands.declare_event_topology"


@override_settings(RABBITMQ_URL=BROKER_URL, EVENTS_EXCHANGE="thesis.events", NOTIFICATIONS_QUEUE="notifications.thesis-events")
class DeclareEventTopologyCommandTests(SimpleTestCase):
    """The command declares the topology once the broker accepts connections."""

    def setUp(self) -> None:
        connection_patcher = patch(f"{COMMAND_MODULE}.pika.BlockingConnection")
        self.connection_class = connection_patcher.start()
        self.addCleanup(connection_patcher.stop)
        sleep_patcher = patch(f"{COMMAND_MODULE}.time.sleep")
        self.sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.connection = self.connection_class.return_value
        self.channel = self.connection.channel.return_value

    def test_declares_topology_and_closes_connection(self) -> None:
        output = io.StringIO()
        call_command("declare_event_topology", stdout=output)
        self.assertEqual(self.channel.exchange_declare.call_count, 2)
        queue_names = [declare_call.kwargs["queue"] for declare_call in self.channel.queue_declare.call_args_list]
        self.assertEqual(
            queue_names,
            [
                "notifications.thesis-events.dlq", "notifications.thesis-events",
                "indexing.thesis-events.dlq", "indexing.thesis-events",
                "adjudication.thesis-events.dlq", "adjudication.thesis-events",
            ],
        )
        self.connection.close.assert_called_once()
        self.assertIn("Topologia declarada: exchange thesis.events, filas notifications.thesis-events, indexing.thesis-events, adjudication.thesis-events.", output.getvalue())
        self.sleep.assert_not_called()

    def test_retries_until_broker_accepts_connections(self) -> None:
        self.connection_class.side_effect = [pika.exceptions.AMQPConnectionError(BROKER_URL), pika.exceptions.AMQPConnectionError(BROKER_URL), self.connection]
        with self.assertLogs(COMMAND_MODULE, level="WARNING") as captured:
            call_command("declare_event_topology", "--retry-delay", "0.5", stdout=io.StringIO())
        self.assertEqual(self.connection_class.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.sleep.assert_called_with(0.5)
        self.assertNotIn("segredo", "\n".join(captured.output))
        self.assertTrue(self.channel.queue_bind.called)

    def test_retries_while_broker_host_does_not_resolve(self) -> None:
        name_resolution_error = socket.gaierror(-3, "Temporary failure in name resolution")
        self.connection_class.side_effect = [name_resolution_error, self.connection]
        with self.assertLogs(COMMAND_MODULE, level="WARNING"):
            call_command("declare_event_topology", stdout=io.StringIO())
        self.assertEqual(self.connection_class.call_count, 2)
        self.connection.close.assert_called_once()

    def test_gives_up_after_all_attempts_without_exposing_url(self) -> None:
        self.connection_class.side_effect = pika.exceptions.AMQPConnectionError(BROKER_URL)
        with self.assertLogs(COMMAND_MODULE, level="WARNING"), self.assertRaises(CommandError) as captured:
            call_command("declare_event_topology", "--attempts", "3")
        self.assertEqual(self.connection_class.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertNotIn("segredo", str(captured.exception))

    def test_rejects_non_positive_attempts(self) -> None:
        with self.assertRaises(CommandError):
            call_command("declare_event_topology", "--attempts", "0")

    @override_settings(RABBITMQ_URL="")
    def test_missing_broker_url_is_command_error(self) -> None:
        with self.assertRaises(CommandError):
            call_command("declare_event_topology")
