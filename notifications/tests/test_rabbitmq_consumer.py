"""
Tests for the pika consumer adapter, its reconnection loop and the ``consume_notifications`` command.
"""

from __future__ import annotations

import signal
import socket
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pika.exceptions
from django.core import mail
from django.core.management import CommandError, call_command
from django.db import OperationalError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from academic.models import Course, Department
from accounts.models import ProfileRole
from events.broker import BrokerUnavailableError
from events.envelope import EventEnvelope
from events.topology import event_topology
from notifications.rabbitmq import NotificationConsumer, run_consumer_loop
from notifications.tests.test_consumer import create_user
from theses.events import record_thesis_published
from theses.models import Thesis, ThesisAuthor, ThesisStatus

BROKER_URL = "amqp://thesis_events:segredo@rabbitmq:5672/thesis_manager"


def delivery(message_id: str = "msg-1") -> tuple[SimpleNamespace, SimpleNamespace]:
    return SimpleNamespace(delivery_tag=7), SimpleNamespace(message_id=message_id)


class HandleDeliveryTests(TestCase):
    """Ack on success or duplicate; dead-letter on invalid, rejected or unexpected; requeue on database outage."""

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        course = Course.objects.create(department=department, name="CC", code="CC001")
        student = create_user("aluno@ufvjm.edu.br", "Ana Aluna", ProfileRole.STUDENT)
        advisor = create_user("orientador@ufvjm.edu.br", "Olga Orientadora", ProfileRole.ADVISOR)
        thesis = Thesis.objects.create(course=course, advisor=advisor, title="Tese", status=ThesisStatus.APPROVED)
        ThesisAuthor.objects.create(thesis=thesis, author=student, position=1, is_main_author=True)
        with transaction.atomic():
            event = record_thesis_published(thesis)
        self.envelope = EventEnvelope.from_outbox(event)
        self.body = self.envelope.to_json_bytes()
        self.channel = MagicMock()
        self.consumer = NotificationConsumer(BROKER_URL, event_topology(), "notifications.thesis-events", 10)
        # O TestCase roda dentro de uma transação (autocommit desligado), e
        # close_old_connections fecharia a conexão do teste; no worker real não há transação aberta.
        patcher = patch("notifications.rabbitmq.close_old_connections")
        self.close_old_connections = patcher.start()
        self.addCleanup(patcher.stop)

    def test_processed_event_is_acked_and_sends_e_mail(self) -> None:
        method, properties = delivery(str(self.envelope.event_id))
        self.consumer.handle_delivery(self.channel, method, properties, self.body)
        self.channel.basic_ack.assert_called_once_with(delivery_tag=7)
        self.channel.basic_nack.assert_not_called()
        self.assertEqual(len(mail.outbox), 2)
        self.close_old_connections.assert_called_once_with()

    def test_duplicate_is_acked_without_new_e_mail(self) -> None:
        method, properties = delivery()
        self.consumer.handle_delivery(self.channel, method, properties, self.body)
        self.consumer.handle_delivery(self.channel, method, properties, self.body)
        self.assertEqual(self.channel.basic_ack.call_args_list, [call(delivery_tag=7), call(delivery_tag=7)])
        self.assertEqual(len(mail.outbox), 2)

    def test_undecodable_body_is_dead_lettered(self) -> None:
        method, properties = delivery()
        with self.assertLogs("notifications.rabbitmq", level="WARNING") as captured:
            self.consumer.handle_delivery(self.channel, method, properties, b'{"email": "aluno@ufvjm.edu.br"')
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
        self.assertNotIn("aluno@ufvjm.edu.br", "\n".join(captured.output))

    def test_rejected_event_is_dead_lettered(self) -> None:
        unknown = EventEnvelope(**{**self.envelope.__dict__, "event_type": "thesis.unknown"})
        method, properties = delivery()
        with self.assertLogs("notifications.consumer", level="WARNING"):
            self.consumer.handle_delivery(self.channel, method, properties, unknown.to_json_bytes())
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
        self.channel.basic_ack.assert_not_called()

    def test_unexpected_error_is_dead_lettered_without_details_in_logs(self) -> None:
        method, properties = delivery()
        error = RuntimeError("duplicate key (email)=(aluno@ufvjm.edu.br)")
        with patch("notifications.rabbitmq.handle_event", side_effect=error), self.assertLogs("notifications.rabbitmq", level="ERROR") as captured:
            self.consumer.handle_delivery(self.channel, method, properties, self.body)
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=False)
        output = "\n".join(captured.output)
        self.assertIn("RuntimeError", output)
        self.assertNotIn("aluno@ufvjm.edu.br", output)

    def test_database_outage_requeues_after_pause(self) -> None:
        method, properties = delivery()
        with patch("notifications.rabbitmq.handle_event", side_effect=OperationalError("server closed the connection")), patch("notifications.rabbitmq.time.sleep") as sleep, self.assertLogs("notifications.rabbitmq", level="WARNING"):
            self.consumer.handle_delivery(self.channel, method, properties, self.body)
        sleep.assert_called_once_with(5.0)
        self.channel.basic_nack.assert_called_once_with(delivery_tag=7, requeue=True)


class ConsumeTests(SimpleTestCase):
    """``consume`` declares the topology, consumes with manual acks and closes on stop."""

    def setUp(self) -> None:
        patcher = patch("notifications.rabbitmq.pika.BlockingConnection")
        self.connection_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.connection = self.connection_class.return_value
        self.connection.is_open = True
        self.channel = self.connection.channel.return_value
        self.consumer = NotificationConsumer(BROKER_URL, event_topology(), "notifications.thesis-events", 10)

    def test_declares_topology_and_consumes_until_stopped(self) -> None:
        stop_event = threading.Event()
        self.connection.process_data_events.side_effect = lambda time_limit: stop_event.set()
        with self.assertLogs("notifications.rabbitmq", level="INFO"):
            self.consumer.consume(stop_event)
        self.assertTrue(self.channel.queue_declare.called)
        self.channel.basic_qos.assert_called_once_with(prefetch_count=10)
        self.channel.basic_consume.assert_called_once_with(queue="notifications.thesis-events", on_message_callback=self.consumer.handle_delivery, auto_ack=False)
        self.connection.close.assert_called_once()

    def test_connection_failure_is_unavailable_without_url(self) -> None:
        self.connection_class.side_effect = pika.exceptions.AMQPConnectionError(BROKER_URL)
        with self.assertRaises(BrokerUnavailableError) as captured:
            self.consumer.consume(threading.Event())
        self.assertNotIn("segredo", str(captured.exception))

    def test_unresolvable_broker_host_is_unavailable_not_a_crash(self) -> None:
        # Regressão: com o container rabbitmq parado, o socket.gaierror derrubava o consumidor.
        self.connection_class.side_effect = socket.gaierror(-3, "Temporary failure in name resolution")
        with self.assertRaises(BrokerUnavailableError):
            self.consumer.consume(threading.Event())

    def test_lost_connection_closes_and_raises_unavailable(self) -> None:
        self.connection.process_data_events.side_effect = pika.exceptions.StreamLostError("lost")
        self.connection.is_open = False
        with self.assertLogs("notifications.rabbitmq", level="INFO"), self.assertRaises(BrokerUnavailableError):
            self.consumer.consume(threading.Event())


class ConsumerLoopTests(SimpleTestCase):
    """The loop reconnects with exponential backoff and stops on request."""

    def test_reconnects_with_backoff_until_stopped(self) -> None:
        consumer = MagicMock()
        consumer.consume.side_effect = [BrokerUnavailableError("down"), BrokerUnavailableError("down"), None]
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False, False, False, True]
        with self.assertLogs("notifications.rabbitmq", level="WARNING"):
            run_consumer_loop(consumer, stop_event, max_reconnect_delay=60.0)
        self.assertEqual(stop_event.wait.call_args_list, [call(2.0), call(4.0)])
        self.assertEqual(consumer.consume.call_count, 3)

    def test_reconnect_delay_is_capped(self) -> None:
        consumer = MagicMock()
        consumer.consume.side_effect = [BrokerUnavailableError("down")] * 4
        stop_event = MagicMock()
        stop_event.is_set.side_effect = [False] * 4 + [True]
        with self.assertLogs("notifications.rabbitmq", level="WARNING"):
            run_consumer_loop(consumer, stop_event, max_reconnect_delay=5.0)
        self.assertEqual(stop_event.wait.call_args_list, [call(2.0), call(4.0), call(5.0), call(5.0)])


class ConsumeCommandTests(SimpleTestCase):
    """The command requires the broker URL and stops cleanly on SIGTERM."""

    @override_settings(RABBITMQ_URL="")
    def test_missing_broker_url_is_command_error(self) -> None:
        with self.assertRaises(CommandError):
            call_command("consume_notifications")

    @override_settings(RABBITMQ_URL=BROKER_URL)
    def test_sigterm_handler_stops_the_loop(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            self.addCleanup(signal.signal, signum, signal.getsignal(signum))
        stop_states: list[bool] = []

        def fake_loop(consumer, stop_event, max_reconnect_delay) -> None:
            handler = signal.getsignal(signal.SIGTERM)
            handler(signal.SIGTERM, None)
            stop_states.append(stop_event.is_set())

        with patch("notifications.management.commands.consume_notifications.run_consumer_loop", side_effect=fake_loop), self.assertLogs("notifications.management.commands.consume_notifications", level="INFO"):
            call_command("consume_notifications")
        self.assertEqual(stop_states, [True])
