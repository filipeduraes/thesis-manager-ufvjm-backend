"""
Management command that declares the RabbitMQ topology before the workers start.
"""

from __future__ import annotations

import logging
import time

import pika
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.rabbitmq import BROKER_CONNECTION_ERRORS
from events.topology import EventTopology, declare_topology, event_topology

logger = logging.getLogger(__name__)

DEFAULT_ATTEMPTS = 10
DEFAULT_RETRY_DELAY_SECONDS = 3.0


class Command(BaseCommand):
    """
    Declare exchanges, consumer queues, dead-letter queues and bindings (idempotent).

    Runs as a one-shot step before the publisher and the consumers, so the first
    published event already finds its queues instead of being returned as unroutable.
    """

    help = "Declara no RabbitMQ as exchanges, filas, DLQs e ligações dos eventos (idempotente)."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """
        parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS, help="Tentativas de conexão antes de desistir.")
        parser.add_argument("--retry-delay", type=float, default=DEFAULT_RETRY_DELAY_SECONDS, help="Segundos entre as tentativas de conexão.")

    def handle(self, *args, **options) -> None:
        """
        Connect to the broker and declare the topology, retrying while it is not ready.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: When ``RABBITMQ_URL`` is missing, arguments are invalid or
                the broker stays unreachable after every attempt.
        """
        if not settings.RABBITMQ_URL:
            raise CommandError("RABBITMQ_URL must be set to declare the event topology.")
        attempts = options["attempts"]
        if attempts < 1:
            raise CommandError("--attempts deve ser maior que zero.")
        topology = event_topology()
        for attempt in range(1, attempts + 1):
            try:
                self._declare(topology)
            except BROKER_CONNECTION_ERRORS as error:
                # Só a classe: exceções de conexão do pika podem conter host e usuário.
                logger.warning("Event broker not ready (attempt %s/%s): %s", attempt, attempts, type(error).__name__)
                if attempt < attempts:
                    time.sleep(options["retry_delay"])
                continue
            queue_names = ", ".join(spec.queue for spec in topology.consumer_queues)
            self.stdout.write(f"Topologia declarada: exchange {topology.exchange}, filas {queue_names}.")
            return
        raise CommandError("Broker de eventos indisponível; topologia não declarada.")

    def _declare(self, topology: EventTopology) -> None:
        """
        Open a connection, declare the topology and close the connection.

        Args:
            topology: Topology to declare.
        """
        parameters = pika.URLParameters(settings.RABBITMQ_URL)
        connection = pika.BlockingConnection(parameters)
        try:
            channel = connection.channel()
            declare_topology(channel, topology)
        finally:
            connection.close()
