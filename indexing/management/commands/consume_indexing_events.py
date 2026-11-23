"""
Management command that runs the text indexing consumer as a long-lived service.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.topology import event_topology, indexing_queue_spec
from indexing.rabbitmq import IndexingConsumer, run_consumer_loop

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY_SECONDS = 60.0


class Command(BaseCommand):
    """
    Consume PDF text extraction events and update the public search index.
    """

    help = "Consome eventos de extração de texto e atualiza o índice de busca."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """

    def handle(self, *args, **options) -> None:
        """
        Run the indexing consumer loop.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: When ``RABBITMQ_URL`` is not configured.
        """
        if not settings.RABBITMQ_URL:
            raise CommandError("RABBITMQ_URL must be set to consume events.")
        queue_spec = indexing_queue_spec()
        consumer = IndexingConsumer(settings.RABBITMQ_URL, event_topology(), queue_spec.queue, settings.INDEXING_PREFETCH)
        stop_event = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            logger.info("Received signal %s; stopping indexing consumer", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        logger.info("Indexing consumer started (queue=%s, prefetch=%s)", queue_spec.queue, settings.INDEXING_PREFETCH)
        run_consumer_loop(consumer, stop_event, MAX_RECONNECT_DELAY_SECONDS)
        logger.info("Indexing consumer stopped")
