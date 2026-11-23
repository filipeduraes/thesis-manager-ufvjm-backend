"""
Retention command for published outbox events and processed-event markers.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandParser
from django.db import connections, transaction

from accounts.concurrency import database_for_model
from django.utils import timezone

from events.models import OutboxEvent, OutboxStatus, ProcessedEvent
from notifications.models import Notification


class Command(BaseCommand):
    """Delete old published events and idempotency markers in bounded batches."""

    help = "Remove eventos publicados e marcadores antigos conforme a política de retenção."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register command arguments."""
        parser.add_argument("--days", type=int, default=30, help="Idade mínima em dias (padrão: 30).")
        parser.add_argument("--batch-size", type=int, default=1000, help="Linhas por lote (padrão: 1000).")
        parser.add_argument("--dry-run", action="store_true", help="Apenas informa quantas linhas seriam removidas.")

    def handle(self, *args, **options) -> None:
        """Run the cleanup."""
        days = options["days"]
        batch_size = options["batch_size"]
        if days < 1:
            self.stderr.write("--days deve ser maior que zero.")
            return
        if batch_size < 1:
            self.stderr.write("--batch-size deve ser maior que zero.")
            return
        cutoff = timezone.now() - timedelta(days=days)
        dry_run = options["dry_run"]

        outbox_qs = OutboxEvent.objects.filter(status=OutboxStatus.COMPLETED, published_at__lt=cutoff)
        processed_qs = ProcessedEvent.objects.filter(processed_at__lt=cutoff)
        outbox_count = outbox_qs.count()
        processed_count = processed_qs.count()
        if dry_run:
            self.stdout.write(f"Dry run: {outbox_count} eventos e {processed_count} marcadores seriam removidos.")
            return

        removed_events = 0
        while True:
            with transaction.atomic():
                ids = list(OutboxEvent.objects.filter(status=OutboxStatus.COMPLETED, published_at__lt=cutoff).values_list("pk", flat=True)[:batch_size])
                if not ids:
                    break
                # O SET_NULL de Notification.event deve rodar no banco das
                # notificações; o collector do Django usaria o alias do evento.
                notification_db = database_for_model(Notification)
                Notification.objects.using(notification_db).filter(event_id__in=ids).update(event_id=None)
                event_db = database_for_model(OutboxEvent)
                removed_events += OutboxEvent.objects.using(event_db).filter(pk__in=ids)._raw_delete(event_db)

        removed_markers = 0
        while True:
            with transaction.atomic():
                keys = list(ProcessedEvent.objects.filter(processed_at__lt=cutoff).values_list("consumer_group", "event_id")[:batch_size])
                if not keys:
                    break
                from django.db.models import Q

                query = Q()
                for group, event_id in keys:
                    query |= Q(consumer_group=group, event_id=event_id)
                removed_markers += ProcessedEvent.objects.filter(query).delete()[0]

        self.stdout.write(f"Removidos {removed_events} eventos e {removed_markers} marcadores anteriores a {cutoff:%Y-%m-%d}.")
