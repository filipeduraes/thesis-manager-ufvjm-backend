"""
System checks for the e-mail configuration used by notifications.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import CheckMessage, Warning, register

# Backends que copiam o corpo da mensagem para a saída padrão (e, portanto, para os logs).
LOGGING_EMAIL_BACKENDS = frozenset({"django.core.mail.backends.console.EmailBackend"})


@register()
def check_email_backend_does_not_log_messages(app_configs: object = None, **kwargs: object) -> list[CheckMessage]:
    """
    Warn when the e-mail backend would write messages, and invitation tokens, to the logs.

    Args:
        app_configs: Apps being checked (unused; the setting is global).
        **kwargs: Extra arguments passed by Django.

    Returns:
        list[CheckMessage]: One warning when a logging backend is configured.
    """
    if settings.EMAIL_BACKEND not in LOGGING_EMAIL_BACKENDS:
        return []
    return [
        Warning(
            "EMAIL_BACKEND imprime os e-mails no log, inclusive o token de acesso dos membros externos da banca.",
            hint="Use o Mailpit do docker-compose em desenvolvimento e o SMTP institucional em produção.",
            id="notifications.W001",
        )
    ]
