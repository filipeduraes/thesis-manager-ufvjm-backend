"""
In-memory broker used by tests instead of a real RabbitMQ.
"""

from __future__ import annotations

from uuid import UUID

from events.broker import BrokerUnavailableError, EventBrokerError
from events.envelope import EventEnvelope


class InMemoryEventBroker:
    """
    Broker double that records published envelopes and can simulate failures.

    Attributes:
        published: Envelopes confirmed so far, in publication order.
        failures: Errors to raise for specific event identifiers.
        unavailable: When True, every publish raises ``BrokerUnavailableError``.
        closed: Whether ``close`` was called.
    """

    def __init__(self) -> None:
        """
        Start with no published events and no simulated failures.
        """
        self.published: list[EventEnvelope] = []
        self.failures: dict[UUID, EventBrokerError] = {}
        self.unavailable = False
        self.closed = False

    def publish(self, envelope: EventEnvelope) -> None:
        """
        Record the envelope or raise the configured failure.

        Args:
            envelope: Event to publish.

        Raises:
            BrokerUnavailableError: When ``unavailable`` is set.
            EventBrokerError: When a failure is configured for this event.
        """
        if self.unavailable:
            raise BrokerUnavailableError("Connection refused")
        failure = self.failures.get(envelope.event_id)
        if failure is not None:
            raise failure
        self.published.append(envelope)

    def close(self) -> None:
        """
        Mark the broker as closed.
        """
        self.closed = True
