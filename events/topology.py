"""
RabbitMQ topology of the event bus: exchanges, consumer queues, dead-letter queues and bindings.

Declarations are idempotent, so every process that needs the topology declares
it at startup. Queue arguments are part of the declaration and cannot change on
an existing queue (RabbitMQ answers PRECONDITION_FAILED); changing them requires
a deliberate migration of the queue.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from pika.adapters.blocking_connection import BlockingChannel

EVENTS_EXCHANGE_TYPE = "topic"
DEAD_LETTER_EXCHANGE_TYPE = "direct"
NOTIFICATION_ROUTING_KEYS: tuple[str, ...] = ("thesis.*", "defense.*")
INDEXING_ROUTING_KEYS: tuple[str, ...] = ("thesis.text_extraction_requested",)
ADJUDICATION_ROUTING_KEYS: tuple[str, ...] = ("defense.evaluation_recorded",)
REPOSITORY_ROUTING_KEYS: tuple[str, ...] = ("thesis.published", "thesis.repository_updated", "thesis.text_extracted")
REPORTS_ROUTING_KEYS: tuple[str, ...] = ("thesis.submitted", "defense.completed", "thesis.defense_result_recorded", "thesis.published")


@dataclass(frozen=True)
class ConsumerQueueSpec:
    """
    Durable queue of one consumer group, with its dead-letter queue.

    Attributes:
        queue: Queue consumed by the group.
        routing_keys: Binding patterns on the events exchange.
        dead_letter_queue: Queue that receives rejected messages.
    """

    queue: str
    routing_keys: tuple[str, ...]
    dead_letter_queue: str


@dataclass(frozen=True)
class EventTopology:
    """
    Complete topology declared on the broker.

    Attributes:
        exchange: Topic exchange that receives every domain event.
        dead_letter_exchange: Direct exchange that routes rejected messages to each DLQ.
        consumer_queues: Queues of the consumer groups.
    """

    exchange: str
    dead_letter_exchange: str
    consumer_queues: tuple[ConsumerQueueSpec, ...]


def notifications_queue_spec() -> ConsumerQueueSpec:
    """
    Return the queue specification of the notification consumer.

    Returns:
        ConsumerQueueSpec: Queue, bindings and DLQ from settings.
    """
    queue = settings.NOTIFICATIONS_QUEUE
    return ConsumerQueueSpec(queue=queue, routing_keys=NOTIFICATION_ROUTING_KEYS, dead_letter_queue=f"{queue}.dlq")


def indexing_queue_spec() -> ConsumerQueueSpec:
    """
    Return the queue specification of the text indexing consumer.

    Returns:
        ConsumerQueueSpec: Queue, binding and DLQ from settings.
    """
    queue = settings.INDEXING_QUEUE
    return ConsumerQueueSpec(queue=queue, routing_keys=INDEXING_ROUTING_KEYS, dead_letter_queue=f"{queue}.dlq")


def adjudication_queue_spec() -> ConsumerQueueSpec:
    """
    Return the queue specification of the defense adjudication consumer.

    Returns:
        ConsumerQueueSpec: Queue, binding and DLQ from settings.
    """
    queue = settings.ADJUDICATION_QUEUE
    return ConsumerQueueSpec(queue=queue, routing_keys=ADJUDICATION_ROUTING_KEYS, dead_letter_queue=f"{queue}.dlq")


def repository_queue_spec() -> ConsumerQueueSpec:
    """
    Return the queue specification of the repository projection consumer.

    Returns:
        ConsumerQueueSpec: Queue, bindings and DLQ from settings.
    """
    queue = settings.REPOSITORY_QUEUE
    return ConsumerQueueSpec(queue=queue, routing_keys=REPOSITORY_ROUTING_KEYS, dead_letter_queue=f"{queue}.dlq")


def reports_queue_spec() -> ConsumerQueueSpec:
    """Return the queue specification of the reporting projection consumer."""
    queue = settings.REPORTS_QUEUE
    return ConsumerQueueSpec(queue=queue, routing_keys=REPORTS_ROUTING_KEYS, dead_letter_queue=f"{queue}.dlq")


def event_topology() -> EventTopology:
    """
    Return the topology of the event bus as configured in settings.

    Returns:
        EventTopology: Exchanges and every consumer queue.
    """
    exchange = settings.EVENTS_EXCHANGE
    return EventTopology(exchange=exchange, dead_letter_exchange=f"{exchange}.dlx", consumer_queues=(notifications_queue_spec(), indexing_queue_spec(), adjudication_queue_spec(), repository_queue_spec(), reports_queue_spec()))


def declare_topology(channel: BlockingChannel, topology: EventTopology) -> None:
    """
    Declare exchanges, queues, dead-letter queues and bindings (idempotent).

    Each consumer queue dead-letters to the DLX with its own name as routing key,
    so rejected messages of one consumer never reach another consumer's DLQ.

    Args:
        channel: Open channel.
        topology: Topology to declare.
    """
    channel.exchange_declare(exchange=topology.exchange, exchange_type=EVENTS_EXCHANGE_TYPE, durable=True)
    channel.exchange_declare(exchange=topology.dead_letter_exchange, exchange_type=DEAD_LETTER_EXCHANGE_TYPE, durable=True)
    for spec in topology.consumer_queues:
        channel.queue_declare(queue=spec.dead_letter_queue, durable=True)
        channel.queue_bind(queue=spec.dead_letter_queue, exchange=topology.dead_letter_exchange, routing_key=spec.queue)
        queue_arguments = {"x-dead-letter-exchange": topology.dead_letter_exchange, "x-dead-letter-routing-key": spec.queue}
        channel.queue_declare(queue=spec.queue, durable=True, arguments=queue_arguments)
        for routing_key in spec.routing_keys:
            channel.queue_bind(queue=spec.queue, exchange=topology.exchange, routing_key=routing_key)
