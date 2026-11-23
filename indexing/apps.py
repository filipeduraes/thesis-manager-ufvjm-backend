"""
Django app configuration of the text indexing service.
"""

from django.apps import AppConfig


class IndexingConfig(AppConfig):
    """
    Register the consumer of PDF text extraction and search indexing events.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "indexing"
    verbose_name = "Indexação de monografias"
