"""
RabbitMQ implementation of the event broker (pika, blocking connection with publisher confirms).
"""

from __future__ import annotations

import logging

import pika
import pika.exceptions
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from pika.adapters.blocking_connection import BlockingChannel

from events.broker import BrokerUnavailableError, EventBroker, EventBrokerError
from events.envelope import ENVELOPE_CONTENT_TYPE, EventEnvelope

logger = logging.getLogger(__name__)

EXCHANGE_TYPE = "topic"


class PikaEventBroker:
    """
    Publish events to a durable topic exchange and wait for each broker confirm.

    The connection is opened lazily and reopened after a failure, so a broker
    restart only interrupts the current batch. Messages are persistent and
    published with ``mandatory=True``: an event with no bound queue is returned
    as unroutable and retried, instead of being silently dropped.
    """

    def __init__(self, url: str, exchange: str) -> None:
        """
        Store the connection settings without connecting yet.

        Args:
            url: AMQP URL with credentials (never logged).
            exchange: Name of the topic exchange that receives the events.
        """
        self._url = url
        self._exchange = exchange
        self._connection: pika.BlockingConnection | None = None
        self._channel: BlockingChannel | None = None

    def _open_channel(self) -> BlockingChannel:
        """
        Return an open channel with confirms enabled, connecting if needed.

        Returns:
            BlockingChannel: Ready channel.

        Raises:
            BrokerUnavailableError: When the broker cannot be reached.
        """
        if self._channel is not None and self._channel.is_open:
            return self._channel
        try:
            if self._connection is None or self._connection.is_closed:
                parameters = pika.URLParameters(self._url)
                self._connection = pika.BlockingConnection(parameters)
            channel = self._connection.channel()
            channel.exchange_declare(exchange=self._exchange, exchange_type=EXCHANGE_TYPE, durable=True)
            channel.confirm_delivery()
        except (pika.exceptions.AMQPConnectionError, pika.exceptions.ConnectionWrongStateError) as exc:
            self._reset()
            # Mensagem fixa: exceções de conexão do pika podem conter host e usuário.
            raise BrokerUnavailableError(f"Could not connect to the event broker ({type(exc).__name__})") from exc
        self._channel = channel
        return channel

    def publish(self, envelope: EventEnvelope) -> None:
        """
        Publish one envelope and wait for the broker confirm.

        Args:
            envelope: Event to publish; ``event_type`` is the routing key.

        Raises:
            BrokerUnavailableError: When the connection cannot be opened or is lost.
            EventBrokerError: When the broker nacks the message, returns it as
                unroutable or closes the channel.
        """
        channel = self._open_channel()
        properties = pika.BasicProperties(
            content_type=ENVELOPE_CONTENT_TYPE,
            delivery_mode=pika.DeliveryMode.Persistent,
            message_id=str(envelope.event_id),
            type=envelope.event_type,
            timestamp=int(envelope.occurred_at.timestamp()),
            headers={"eventVersion": envelope.event_version},
        )
        body = envelope.to_json_bytes()
        try:
            channel.basic_publish(exchange=self._exchange, routing_key=envelope.event_type, body=body, properties=properties, mandatory=True)
        except pika.exceptions.UnroutableError as exc:
            raise EventBrokerError(f"Event returned as unroutable by exchange {self._exchange} (no bound queue)") from exc
        except pika.exceptions.NackError as exc:
            raise EventBrokerError("Event negatively acknowledged by the broker") from exc
        except (pika.exceptions.AMQPConnectionError, pika.exceptions.ConnectionWrongStateError) as exc:
            self._reset()
            raise BrokerUnavailableError(f"Connection to the event broker lost ({type(exc).__name__})") from exc
        except pika.exceptions.AMQPChannelError as exc:
            self._channel = None
            raise EventBrokerError(f"Broker channel closed while publishing ({type(exc).__name__})") from exc

    def _reset(self) -> None:
        """
        Forget the current connection and channel so the next publish reconnects.
        """
        connection = self._connection
        self._connection = None
        self._channel = None
        if connection is not None and connection.is_open:
            try:
                connection.close()
            except pika.exceptions.AMQPError:
                logger.debug("Ignoring error while closing a broken broker connection")

    def close(self) -> None:
        """
        Close the connection, if open.
        """
        self._reset()


def build_event_broker() -> EventBroker:
    """
    Build the broker client from settings.

    Returns:
        EventBroker: RabbitMQ broker client (not connected yet).

    Raises:
        ImproperlyConfigured: When ``RABBITMQ_URL`` is not set.
    """
    if not settings.RABBITMQ_URL:
        raise ImproperlyConfigured("RABBITMQ_URL must be set to publish events.")
    return PikaEventBroker(settings.RABBITMQ_URL, settings.EVENTS_EXCHANGE)
