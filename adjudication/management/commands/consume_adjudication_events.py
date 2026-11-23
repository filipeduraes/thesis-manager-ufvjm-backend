"""
Management command that runs the defense adjudication consumer as a long-lived service.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.topology import event_topology, adjudication_queue_spec
from adjudication.rabbitmq import AdjudicationConsumer, run_consumer_loop

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY_SECONDS = 60.0


class Command(BaseCommand):
    """
    Consume evaluation events and calculate defense results asynchronously.
    """

    help = "Consome eventos de avaliação e calcula o resultado das bancas."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """

    def handle(self, *args, **options) -> None:
        """
        Run the adjudication consumer loop.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: When ``RABBITMQ_URL`` is not configured.
        """
        if not settings.RABBITMQ_URL:
            raise CommandError("RABBITMQ_URL must be set to consume events.")
        queue_spec = adjudication_queue_spec()
        consumer = AdjudicationConsumer(settings.RABBITMQ_URL, event_topology(), queue_spec.queue, settings.ADJUDICATION_PREFETCH)
        stop_event = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            logger.info("Received signal %s; stopping adjudication consumer", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        logger.info("Adjudication consumer started (queue=%s, prefetch=%s)", queue_spec.queue, settings.ADJUDICATION_PREFETCH)
        run_consumer_loop(consumer, stop_event, MAX_RECONNECT_DELAY_SECONDS)
        logger.info("Adjudication consumer stopped")
