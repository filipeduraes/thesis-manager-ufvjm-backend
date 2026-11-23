"""
Configuração isolada para a suíte End-to-End.

Não depende de PostgreSQL, Redis nem RabbitMQ: usa SQLite, cache em memória e
backend de e-mail em memória. DEBUG permanece ligado para liberar o login de
teste (`/api/v1/auth/dev-login`) e o frontend pode ser iniciado com `npm start`.

Uso:
    DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e \
        python manage.py migrate --noinput
    DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e \
        python manage.py runserver 127.0.0.1:8000
"""
from pathlib import Path

from .settings import *  # noqa: F401,F403

DEBUG = True

_E2E_SQLITE = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": BASE_DIR / "e2e.sqlite3",  # noqa: F405
    # Requisições concorrentes (ex.: visualizador de PDF) disputam a escrita
    # do log de auditoria; o timeout evita "database is locked" no E2E.
    "OPTIONS": {"timeout": 30, "init_command": "PRAGMA journal_mode=WAL;"},
}
DATABASES = {"default": _E2E_SQLITE}
for _e2e_alias in SERVICE_DATABASE_ALIASES:  # noqa: F405
    DATABASES[_e2e_alias] = dict(_E2E_SQLITE)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "thesis-manager-e2e",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
RABBITMQ_URL = ""
CORS_ALLOWED_ORIGINS = ["http://localhost:3001", "http://127.0.0.1:3001"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:3001", "http://127.0.0.1:3001"]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
MEDIA_ROOT = Path("/tmp/opencode/e2e-media")
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

# O worker de extração roda em processo separado com cache em memória; o marker
# de upload pode ficar órfão. O ambiente E2E não precisa dessa proteção.
UPLOAD_TRACKING_ENABLED = False
