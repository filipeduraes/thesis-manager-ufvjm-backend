"""
Tests for the RabbitMQ topology declaration (channel mocked).
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from django.test import SimpleTestCase, override_settings

from events.topology import declare_topology, event_topology


@override_settings(EVENTS_EXCHANGE="thesis.events", NOTIFICATIONS_QUEUE="notifications.thesis-events")
class EventTopologyTests(SimpleTestCase):
    """The topology declares durable exchanges, the consumer queue with its DLQ and the bindings."""

    def test_topology_names_follow_settings(self) -> None:
        topology = event_topology()
        self.assertEqual(topology.exchange, "thesis.events")
        self.assertEqual(topology.dead_letter_exchange, "thesis.events.dlx")
        [queue_spec] = topology.consumer_queues
        self.assertEqual(queue_spec.queue, "notifications.thesis-events")
        self.assertEqual(queue_spec.dead_letter_queue, "notifications.thesis-events.dlq")
        self.assertEqual(queue_spec.routing_keys, ("thesis.*", "defense.*"))

    def test_declares_exchanges_queues_and_bindings(self) -> None:
        channel = MagicMock()
        declare_topology(channel, event_topology())
        self.assertEqual(
            channel.exchange_declare.call_args_list,
            [
                call(exchange="thesis.events", exchange_type="topic", durable=True),
                call(exchange="thesis.events.dlx", exchange_type="direct", durable=True),
            ],
        )
        self.assertEqual(
            channel.queue_declare.call_args_list,
            [
                call(queue="notifications.thesis-events.dlq", durable=True),
                call(queue="notifications.thesis-events", durable=True, arguments={"x-dead-letter-exchange": "thesis.events.dlx", "x-dead-letter-routing-key": "notifications.thesis-events"}),
            ],
        )
        self.assertEqual(
            channel.queue_bind.call_args_list,
            [
                call(queue="notifications.thesis-events.dlq", exchange="thesis.events.dlx", routing_key="notifications.thesis-events"),
                call(queue="notifications.thesis-events", exchange="thesis.events", routing_key="thesis.*"),
                call(queue="notifications.thesis-events", exchange="thesis.events", routing_key="defense.*"),
            ],
        )
