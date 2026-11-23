"""
Django app configuration of the asynchronous defense adjudication service.
"""

from django.apps import AppConfig


class AdjudicationConfig(AppConfig):
    """
    Register the consumer that calculates defense results from recorded events.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "adjudication"
    verbose_name = "Apuração de bancas"
