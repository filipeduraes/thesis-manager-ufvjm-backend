"""Django app configuration of the public repository read model."""

from django.apps import AppConfig


class RepositoryReadModelConfig(AppConfig):
    """Register the repository projection tables and consumer."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "repository_readmodel"
    verbose_name = "Repositório público (read model)"
