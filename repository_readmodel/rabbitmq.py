"""
RabbitMQ adapter of the repository consumer (pika, blocking connection, manual acks).
"""

from __future__ import annotations

import logging
import threading
import time

import pika
from django.db import InterfaceError, OperationalError, close_old_connections
from pika.adapters.blocking_connection import BlockingChannel
from pika.spec import Basic, BasicProperties

from events.broker import BrokerUnavailableError
from events.envelope import EventEnvelope, InvalidEnvelopeError
from events.rabbitmq import BROKER_CONNECTION_ERRORS
from events.topology import EventTopology, declare_topology
from repository_readmodel.projections import handle_event

logger = logging.getLogger(__name__)

DATABASE_RETRY_DELAY_SECONDS = 5.0
PROCESS_EVENTS_TIME_LIMIT_SECONDS = 1.0


class RepositoryConsumer:
    """
    Consume repository events and hand them to the repository handler.

    Ack policy mirrors the notification consumer: processed/skipped events are
    acked, invalid or unexpected failures are dead-lettered, and a database
    outage requeues the message after a short pause.
    """

    def __init__(self, url: str, topology: EventTopology, queue: str, prefetch_count: int) -> None:
        """
        Store connection settings without connecting yet.

        Args:
            url: AMQP URL with credentials (never logged).
            topology: Topology declared before consuming.
            queue: Queue consumed by this process.
            prefetch_count: Maximum unacknowledged messages delivered at once.
        """
        self._url = url
        self._topology = topology
        self._queue = queue
        self._prefetch_count = prefetch_count

    def handle_delivery(self, channel: BlockingChannel, method: Basic.Deliver, properties: BasicProperties, body: bytes) -> None:
        """
        Process one delivery and ack or nack it.

        Args:
            channel: Channel the message arrived on.
            method: Delivery metadata (delivery tag).
            properties: Message properties (``message_id`` is the event id).
            body: Raw message body.
        """
        delivery_tag = method.delivery_tag
        message_id = properties.message_id or "unknown"
        close_old_connections()
        try:
            envelope = EventEnvelope.from_json_bytes(body)
        except InvalidEnvelopeError as error:
            logger.warning("Dead-lettering undecodable repository message %s: %s", message_id, error)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return
        try:
            outcome = handle_event(envelope)
        except (OperationalError, InterfaceError) as error:
            logger.warning("Database unavailable while repository event %s (%s); requeueing", envelope.event_id, type(error).__name__)
            close_old_connections()
            time.sleep(DATABASE_RETRY_DELAY_SECONDS)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=True)
            return
        except Exception as error:
            logger.error("Dead-lettering repository event %s (%s) after unexpected error: %s", envelope.event_id, envelope.event_type, type(error).__name__)
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return
        if outcome == "REJECTED":
            channel.basic_nack(delivery_tag=delivery_tag, requeue=False)
            return
        channel.basic_ack(delivery_tag=delivery_tag)

    def consume(self, stop_event: threading.Event) -> None:
        """
        Connect, declare the topology and consume until ``stop_event`` is set.

        Args:
            stop_event: Set by the signal handler to stop consuming.

        Raises:
            BrokerUnavailableError: When the connection cannot be opened or is lost.
        """
        connection: pika.BlockingConnection | None = None
        try:
            parameters = pika.URLParameters(self._url)
            connection = pika.BlockingConnection(parameters)
            channel = connection.channel()
            declare_topology(channel, self._topology)
            channel.basic_qos(prefetch_count=self._prefetch_count)
            channel.basic_consume(queue=self._queue, on_message_callback=self.handle_delivery, auto_ack=False)
            logger.info("Repository consumer listening on queue %s", self._queue)
            while not stop_event.is_set():
                connection.process_data_events(time_limit=PROCESS_EVENTS_TIME_LIMIT_SECONDS)
        except BROKER_CONNECTION_ERRORS as error:
            raise BrokerUnavailableError(f"Event broker connection failed ({type(error).__name__})") from error
        finally:
            if connection is not None and connection.is_open:
                connection.close()


def run_consumer_loop(consumer: RepositoryConsumer, stop_event: threading.Event, max_reconnect_delay: float) -> None:
    """
    Keep the repository consumer running with exponential backoff on broker outages.

    Args:
        consumer: Consumer to run.
        stop_event: Set by the signal handler to stop.
        max_reconnect_delay: Upper bound, in seconds, of the wait between reconnections.
    """
    failures = 0
    while not stop_event.is_set():
        try:
            consumer.consume(stop_event)
            failures = 0
        except BrokerUnavailableError as error:
            failures += 1
            reconnect_delay = min(float(2 ** failures), max_reconnect_delay)
            logger.warning("%s; reconnecting in %.0f s", error, reconnect_delay)
            stop_event.wait(reconnect_delay)
