"""Django app configuration of the reporting read model."""

from django.apps import AppConfig


class ReportsReadModelConfig(AppConfig):
    """Register the reporting facts and their projection consumer."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reports_readmodel"
    verbose_name = "Relatórios (read model)"
