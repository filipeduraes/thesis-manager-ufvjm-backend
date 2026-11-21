"""
Tests for the pika broker adapter, with ``pika.BlockingConnection`` mocked (no socket is opened).
"""

from __future__ import annotations

import socket
import uuid
from unittest.mock import patch

import pika.exceptions
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from events.broker import BrokerUnavailableError, EventBrokerError
from events.envelope import EventEnvelope
from events.models import OutboxStatus
from events.publisher import publish_pending_events
from events.rabbitmq import PikaEventBroker, build_event_broker
from events.tests.test_publisher import create_event

BROKER_URL = "amqp://thesis_events:segredo@rabbitmq:5672/thesis_manager"


def build_envelope() -> EventEnvelope:
    thesis_id = uuid.uuid4()
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type="thesis.published",
        event_version="1.0",
        occurred_at=timezone.now(),
        aggregate_type="Thesis",
        aggregate_id=thesis_id,
        correlation_id=None,
        causation_id=None,
        payload={"thesisId": str(thesis_id), "referenceCode": "TCC-1", "publishedAt": "2026-11-19T12:00:00+00:00"},
    )


class PikaEventBrokerTests(SimpleTestCase):
    """The adapter declares the exchange, uses confirms and maps pika errors to broker errors."""

    def setUp(self) -> None:
        patcher = patch("events.rabbitmq.pika.BlockingConnection")
        self.connection_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = self.connection_class.return_value
        self.connection.is_closed = False
        self.connection.is_open = True
        self.channel = self.connection.channel.return_value
        self.channel.is_open = True
        self.broker = PikaEventBroker(BROKER_URL, "thesis.events")

    def test_does_not_connect_until_first_publish(self) -> None:
        self.connection_class.assert_not_called()

    def test_publish_declares_exchange_enables_confirms_and_sets_properties(self) -> None:
        envelope = build_envelope()
        self.broker.publish(envelope)
        self.channel.exchange_declare.assert_called_once_with(exchange="thesis.events", exchange_type="topic", durable=True)
        self.channel.confirm_delivery.assert_called_once_with()
        publish_kwargs = self.channel.basic_publish.call_args.kwargs
        self.assertEqual(publish_kwargs["exchange"], "thesis.events")
        self.assertEqual(publish_kwargs["routing_key"], "thesis.published")
        self.assertTrue(publish_kwargs["mandatory"])
        self.assertEqual(publish_kwargs["body"], envelope.to_json_bytes())
        properties = publish_kwargs["properties"]
        self.assertEqual(properties.delivery_mode, 2)
        self.assertEqual(properties.content_type, "application/json")
        self.assertEqual(properties.message_id, str(envelope.event_id))
        self.assertEqual(properties.type, "thesis.published")
        self.assertEqual(properties.headers, {"eventVersion": "1.0"})

    def test_reuses_open_channel(self) -> None:
        self.broker.publish(build_envelope())
        self.broker.publish(build_envelope())
        self.connection_class.assert_called_once()
        self.connection.channel.assert_called_once()

    def test_connection_failure_raises_unavailable_without_url(self) -> None:
        self.connection_class.side_effect = pika.exceptions.AMQPConnectionError(BROKER_URL)
        with self.assertRaises(BrokerUnavailableError) as captured:
            self.broker.publish(build_envelope())
        self.assertNotIn("segredo", str(captured.exception))
        self.assertNotIn("amqp://", str(captured.exception))

    def test_unresolvable_host_raises_unavailable(self) -> None:
        # Regressão: com o container rabbitmq parado, o pika deixa escapar o socket.gaierror do resolvedor.
        self.connection_class.side_effect = socket.gaierror(-3, "Temporary failure in name resolution")
        with self.assertRaises(BrokerUnavailableError) as captured:
            self.broker.publish(build_envelope())
        self.assertIn("gaierror", str(captured.exception))

    def test_socket_reset_while_publishing_raises_unavailable(self) -> None:
        self.channel.basic_publish.side_effect = ConnectionResetError(104, "Connection reset by peer")
        with self.assertRaises(BrokerUnavailableError):
            self.broker.publish(build_envelope())

    def test_unroutable_and_nack_are_message_failures(self) -> None:
        for error in (pika.exceptions.UnroutableError([]), pika.exceptions.NackError([])):
            self.channel.basic_publish.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(EventBrokerError) as captured:
                self.broker.publish(build_envelope())
            self.assertNotIsInstance(captured.exception, BrokerUnavailableError)

    def test_lost_connection_is_unavailable_and_reconnects_next_time(self) -> None:
        self.channel.basic_publish.side_effect = [pika.exceptions.StreamLostError("lost"), None]
        with self.assertRaises(BrokerUnavailableError):
            self.broker.publish(build_envelope())
        self.connection.close.assert_called_once()
        self.broker.publish(build_envelope())
        self.assertEqual(self.connection_class.call_count, 2)

    def test_closed_channel_is_message_failure_and_reopens_channel(self) -> None:
        self.channel.basic_publish.side_effect = [pika.exceptions.ChannelClosedByBroker(406, "PRECONDITION_FAILED"), None]
        with self.assertRaises(EventBrokerError) as captured:
            self.broker.publish(build_envelope())
        self.assertNotIsInstance(captured.exception, BrokerUnavailableError)
        self.broker.publish(build_envelope())
        self.assertEqual(self.connection.channel.call_count, 2)
        self.connection_class.assert_called_once()

    def test_keep_alive_without_connection_does_nothing(self) -> None:
        self.broker.keep_alive()
        self.connection_class.assert_not_called()
        self.connection.process_data_events.assert_not_called()

    def test_keep_alive_services_heartbeats_on_idle_connection(self) -> None:
        self.broker.publish(build_envelope())
        self.broker.keep_alive()
        self.connection.process_data_events.assert_called_once_with(time_limit=0)

    def test_connection_dropped_while_idle_is_reopened_without_outage(self) -> None:
        # Regressão: o broker fecha a conexão ociosa ("missed heartbeats from client");
        # a próxima publicação não pode virar BrokerUnavailableError.
        self.broker.publish(build_envelope())
        self.connection.process_data_events.side_effect = pika.exceptions.StreamLostError("missed heartbeats")
        with self.assertLogs("events.rabbitmq", level="INFO"):
            self.broker.keep_alive()
        self.connection.process_data_events.side_effect = None
        self.broker.publish(build_envelope())
        self.assertEqual(self.connection_class.call_count, 2)
        self.assertEqual(self.channel.basic_publish.call_count, 2)

    def test_close_closes_open_connection(self) -> None:
        self.broker.publish(build_envelope())
        self.broker.close()
        self.connection.close.assert_called_once()


class PikaBrokerOutageBatchTests(TestCase):
    """With the broker host gone, a batch stops cleanly instead of crashing the publisher."""

    def test_batch_with_unresolvable_broker_keeps_events_pending(self) -> None:
        event = create_event()
        broker = PikaEventBroker(BROKER_URL, "thesis.events")
        name_resolution_error = socket.gaierror(-3, "Temporary failure in name resolution")
        with patch("events.rabbitmq.pika.BlockingConnection", side_effect=name_resolution_error), self.assertLogs("events.publisher", level="WARNING"):
            summary = publish_pending_events(broker, batch_size=10)
        self.assertTrue(summary.broker_unavailable)
        event.refresh_from_db()
        self.assertEqual((event.status, event.attempts, event.last_error), (OutboxStatus.PENDING, 0, ""))


class BuildEventBrokerTests(SimpleTestCase):
    """The broker factory requires the URL setting."""

    @override_settings(RABBITMQ_URL="")
    def test_missing_url_is_improperly_configured(self) -> None:
        with self.assertRaises(ImproperlyConfigured):
            build_event_broker()

    @override_settings(RABBITMQ_URL=BROKER_URL, EVENTS_EXCHANGE="custom.exchange")
    def test_builds_pika_broker_from_settings(self) -> None:
        broker = build_event_broker()
        self.assertIsInstance(broker, PikaEventBroker)
        self.assertEqual(broker._exchange, "custom.exchange")
