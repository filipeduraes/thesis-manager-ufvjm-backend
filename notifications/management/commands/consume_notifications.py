"""
Management command that runs the notification consumer as a long-lived service.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.topology import event_topology, notifications_queue_spec
from notifications.delivery import retry_pending_notifications
from notifications.rabbitmq import NotificationConsumer, run_consumer_loop

logger = logging.getLogger(__name__)

DEFAULT_RETRY_LIMIT = 100
MAX_RECONNECT_DELAY_SECONDS = 60.0


class Command(BaseCommand):
    """
    Consume domain events and send e-mail notifications, or resend failed ones.
    """

    help = "Consome os eventos do RabbitMQ e envia as notificações por e-mail (EMAIL_BACKEND)."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """
        parser.add_argument("--retry-pending", action="store_true", help="Reenvia notificações FAILED ou PENDING há mais de 5 minutos e encerra, sem conectar ao RabbitMQ.")
        parser.add_argument("--limit", type=int, default=DEFAULT_RETRY_LIMIT, help="Máximo de notificações reenviadas com --retry-pending.")

    def handle(self, *args, **options) -> None:
        """
        Run the requested mode.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: On invalid arguments or missing ``RABBITMQ_URL``.
        """
        if options["retry_pending"]:
            self._retry_pending(options["limit"])
            return
        if not settings.RABBITMQ_URL:
            raise CommandError("RABBITMQ_URL must be set to consume events.")
        queue_spec = notifications_queue_spec()
        consumer = NotificationConsumer(settings.RABBITMQ_URL, event_topology(), queue_spec.queue, settings.NOTIFICATIONS_PREFETCH)
        stop_event = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            logger.info("Received signal %s; stopping notification consumer", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        logger.info("Notification consumer started (queue=%s, prefetch=%s)", queue_spec.queue, settings.NOTIFICATIONS_PREFETCH)
        run_consumer_loop(consumer, stop_event, MAX_RECONNECT_DELAY_SECONDS)
        logger.info("Notification consumer stopped")

    def _retry_pending(self, limit: int) -> None:
        """
        Resend failed or stale pending notifications once.

        Args:
            limit: Maximum number of notifications.

        Raises:
            CommandError: When the limit is not positive.
        """
        if limit < 1:
            raise CommandError("--limit deve ser maior que zero.")
        summary = retry_pending_notifications(limit)
        self.stdout.write(f"Reenviadas: {summary.sent}; falharam novamente: {summary.failed}.")
