"""
Tests for the RabbitMQ topology declaration (channel mocked).
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

from django.test import SimpleTestCase, override_settings

from events.topology import declare_topology, event_topology


@override_settings(
    EVENTS_EXCHANGE="thesis.events",
    NOTIFICATIONS_QUEUE="notifications.thesis-events",
    INDEXING_QUEUE="indexing.thesis-events",
    ADJUDICATION_QUEUE="adjudication.thesis-events",
    REPOSITORY_QUEUE="repository.thesis-events",
    REPORTS_QUEUE="reports.thesis-events",
)
class EventTopologyTests(SimpleTestCase):
    """The topology declares durable exchanges and one queue plus DLQ per consumer."""

    def test_topology_names_follow_settings(self) -> None:
        topology = event_topology()
        self.assertEqual(topology.exchange, "thesis.events")
        self.assertEqual(topology.dead_letter_exchange, "thesis.events.dlx")
        queues = {spec.queue: spec for spec in topology.consumer_queues}
        self.assertEqual(set(queues), {"notifications.thesis-events", "indexing.thesis-events", "adjudication.thesis-events", "repository.thesis-events", "reports.thesis-events"})
        self.assertEqual(queues["notifications.thesis-events"].routing_keys, ("thesis.*", "defense.*"))
        self.assertEqual(queues["indexing.thesis-events"].routing_keys, ("thesis.text_extraction_requested",))
        self.assertEqual(queues["adjudication.thesis-events"].routing_keys, ("defense.evaluation_recorded",))
        for spec in queues.values():
            self.assertEqual(spec.dead_letter_queue, f"{spec.queue}.dlq")

    def test_declares_exchanges_queues_and_bindings(self) -> None:
        channel = MagicMock()
        topology = event_topology()
        declare_topology(channel, topology)
        self.assertEqual(
            channel.exchange_declare.call_args_list,
            [
                call(exchange="thesis.events", exchange_type="topic", durable=True),
                call(exchange="thesis.events.dlx", exchange_type="direct", durable=True),
            ],
        )
        declared = {call_args.kwargs["queue"] for call_args in channel.queue_declare.call_args_list}
        expected_declared = set()
        for spec in topology.consumer_queues:
            expected_declared.update({spec.queue, spec.dead_letter_queue})
        self.assertEqual(declared, expected_declared)
        bindings = {(item.kwargs["queue"], item.kwargs["routing_key"]) for item in channel.queue_bind.call_args_list}
        expected_bindings = set()
        for spec in topology.consumer_queues:
            expected_bindings.add((spec.dead_letter_queue, spec.queue))
            expected_bindings.update((spec.queue, routing_key) for routing_key in spec.routing_keys)
        self.assertEqual(bindings, expected_bindings)
