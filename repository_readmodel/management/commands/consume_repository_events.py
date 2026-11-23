"""
Management command that runs the repository projection consumer as a long-lived service.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.topology import event_topology, repository_queue_spec
from repository_readmodel.rabbitmq import RepositoryConsumer, run_consumer_loop

logger = logging.getLogger(__name__)

MAX_RECONNECT_DELAY_SECONDS = 60.0


class Command(BaseCommand):
    """
    Consume PDF text extraction events and update the public search index.
    """

    help = "Consome eventos e atualiza o repositório público."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """

    def handle(self, *args, **options) -> None:
        """
        Run the repository consumer loop.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: When ``RABBITMQ_URL`` is not configured.
        """
        if not settings.RABBITMQ_URL:
            raise CommandError("RABBITMQ_URL must be set to consume events.")
        queue_spec = repository_queue_spec()
        consumer = RepositoryConsumer(settings.RABBITMQ_URL, event_topology(), queue_spec.queue, settings.REPOSITORY_PREFETCH)
        stop_event = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            logger.info("Received signal %s; stopping repository consumer", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        logger.info("Repository consumer started (queue=%s, prefetch=%s)", queue_spec.queue, settings.REPOSITORY_PREFETCH)
        run_consumer_loop(consumer, stop_event, MAX_RECONNECT_DELAY_SECONDS)
        logger.info("Repository consumer stopped")
