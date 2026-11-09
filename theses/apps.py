from django.apps import AppConfig


class ThesesConfig(AppConfig):
    """
    Configuration for the thesis submission and review application.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "theses"
    verbose_name = "Monografias"
