from django.apps import AppConfig


class AcademicConfig(AppConfig):
    """
    Configuration for the academic registry application.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "academic"
    verbose_name = "Cadastro acadêmico"
