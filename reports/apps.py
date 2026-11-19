from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """
    Configuration for the read-only reporting application (dashboards and indicators).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "reports"
    verbose_name = "Relatórios"
