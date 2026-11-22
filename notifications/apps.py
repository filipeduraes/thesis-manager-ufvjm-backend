from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    """
    Configuration for the notification service (templates, deliveries and the event consumer).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "notifications"
    verbose_name = "Notificações"

    def ready(self) -> None:
        """
        Register the e-mail configuration checks.
        """
        from notifications import checks  # noqa: F401
