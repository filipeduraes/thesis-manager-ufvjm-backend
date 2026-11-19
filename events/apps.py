from django.apps import AppConfig


class EventsConfig(AppConfig):
    """
    Configuration for the event bus application (transactional outbox and consumer idempotency).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "events"
    verbose_name = "Eventos"
