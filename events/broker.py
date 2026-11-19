"""
Broker abstraction used by the outbox publisher.
"""

from __future__ import annotations

from typing import Protocol

from events.envelope import EventEnvelope


class EventBrokerError(Exception):
    """
    The broker did not accept one message (negative confirm, unroutable, encoding error).

    The publisher counts an attempt for the event and schedules a retry.
    """


class BrokerUnavailableError(EventBrokerError):
    """
    The broker cannot be reached at all (connection refused or lost).

    The publisher stops the batch without counting attempts: an outage is not a
    problem of the events themselves and must not push them to ``FAILED``.
    """


class EventBroker(Protocol):
    """
    Destination of outbox events; implementations must only return after the broker confirmed the message.
    """

    def publish(self, envelope: EventEnvelope) -> None:
        """
        Publish one event and wait for the broker confirmation.

        Args:
            envelope: Event to publish.

        Raises:
            BrokerUnavailableError: When the broker cannot be reached.
            EventBrokerError: When the broker rejected this message.
        """
        ...

    def close(self) -> None:
        """
        Release the connection held by the broker client, if any.
        """
        ...
