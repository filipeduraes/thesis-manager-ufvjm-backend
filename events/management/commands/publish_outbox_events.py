"""
Management command that runs the outbox publisher as a long-lived service.
"""

from __future__ import annotations

import logging
import signal
import threading
from types import FrameType
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser

from events.broker import EventBroker
from events.publisher import publish_pending_events, requeue_failed_events, run_publisher_loop
from events.rabbitmq import build_event_broker

logger = logging.getLogger(__name__)

REQUEUE_ALL = "all"


class Command(BaseCommand):
    """
    Publish pending outbox events to RabbitMQ (loop, single batch or manual requeue).
    """

    help = "Publica no RabbitMQ os eventos pendentes da outbox, com novas tentativas e backoff."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """
        parser.add_argument("--once", action="store_true", help="Publica um único lote e encerra (depuração).")
        parser.add_argument("--batch-size", type=int, default=None, help="Máximo de eventos por lote (padrão: OUTBOX_BATCH_SIZE).")
        parser.add_argument("--requeue-failed", metavar="EVENT_ID|all", default=None, help="Volta eventos FAILED para PENDING, zerando as tentativas, e encerra sem publicar.")

    def handle(self, *args, **options) -> None:
        """
        Run the requested publisher mode.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: On invalid arguments, missing configuration or, with
                ``--once``, when the broker is unreachable.
        """
        if options["requeue_failed"] is not None:
            self._requeue(options["requeue_failed"])
            return
        batch_size = options["batch_size"]
        if batch_size is None:
            batch_size = settings.OUTBOX_BATCH_SIZE
        if batch_size < 1:
            raise CommandError("--batch-size deve ser maior que zero.")
        try:
            broker = build_event_broker()
        except ImproperlyConfigured as exc:
            raise CommandError(str(exc)) from exc
        try:
            if options["once"]:
                self._publish_once(broker, batch_size)
            else:
                self._run_forever(broker, batch_size)
        finally:
            broker.close()

    def _requeue(self, target: str) -> None:
        """
        Requeue one failed event or all of them.

        Args:
            target: Event UUID or ``"all"``.

        Raises:
            CommandError: When the identifier is invalid or the event is not ``FAILED``.
        """
        event_id: UUID | None = None
        if target != REQUEUE_ALL:
            try:
                event_id = UUID(target)
            except ValueError as exc:
                raise CommandError("Informe o UUID de um evento ou 'all'.") from exc
        requeued = requeue_failed_events(event_id)
        if event_id is not None and requeued == 0:
            raise CommandError("Evento não encontrado ou não está em FAILED.")
        self.stdout.write(f"{requeued} evento(s) devolvido(s) para PENDING.")

    def _publish_once(self, broker: EventBroker, batch_size: int) -> None:
        """
        Publish a single batch and report the counters.

        Args:
            broker: Broker client.
            batch_size: Maximum number of events.

        Raises:
            CommandError: When the broker was unreachable.
        """
        summary = publish_pending_events(broker, batch_size)
        self.stdout.write(f"Publicados: {summary.published}; reagendados: {summary.retrying}; falhos: {summary.failed}.")
        if summary.broker_unavailable:
            raise CommandError("Broker de eventos indisponível; lote interrompido.")

    def _run_forever(self, broker: EventBroker, batch_size: int) -> None:
        """
        Run the publisher loop until SIGTERM or SIGINT.

        Args:
            broker: Broker client.
            batch_size: Maximum number of events per batch.
        """
        stop_event = threading.Event()

        def request_stop(signum: int, frame: FrameType | None) -> None:
            logger.info("Received signal %s; stopping outbox publisher after the current batch", signum)
            stop_event.set()

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        poll_interval = settings.OUTBOX_POLL_INTERVAL_SECONDS
        logger.info("Outbox publisher started (batch_size=%s, poll_interval=%ss, exchange=%s)", batch_size, poll_interval, settings.EVENTS_EXCHANGE)
        run_publisher_loop(broker, stop_event, batch_size, poll_interval)
        logger.info("Outbox publisher stopped")
