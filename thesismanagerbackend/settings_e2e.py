"""
Isolated configuration for the end-to-end suite.

It does not depend on PostgreSQL, Redis or RabbitMQ: it uses SQLite, an in-memory
cache and an in-memory e-mail backend. DEBUG stays enabled for the development
login (`/api/v1/auth/dev-login`) and the frontend can be started with `npm start`.

Usage:
    DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e \
        python manage.py migrate --noinput
    DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings_e2e \
        python manage.py runserver 127.0.0.1:8000
"""
import os
from pathlib import Path

from .settings import *  # noqa: F401,F403

DEBUG = True

# Cada processo de teste recebe banco, cache e diretório de mídia próprios. Assim
# dois agentes rodando a suíte ao mesmo tempo não apagam os arquivos um do outro
# nem compartilham contadores de throttle. O Playwright define E2E_RUN_ID
# explicitamente porque `migrate` e `runserver` são processos diferentes.
_E2E_RUN_ID = os.environ.get("E2E_RUN_ID", str(os.getpid()))
_E2E_DB_NAME = Path(os.environ.get("E2E_DB_NAME", str(BASE_DIR / f"e2e-{_E2E_RUN_ID}.sqlite3")))  # noqa: F405
_E2E_MEDIA_ROOT = Path(os.environ.get("E2E_MEDIA_ROOT", f"/tmp/opencode/e2e-media-{_E2E_RUN_ID}"))

_E2E_SQLITE = {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": _E2E_DB_NAME,
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
        "LOCATION": f"thesis-manager-e2e-{_E2E_RUN_ID}",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"
# A suíte E2E faz dezenas de logins por minuto via /auth/dev-login (escopo "session");
# o limite normal geraria 429 falsos. Os demais escopos permanecem intactos para que
# os testes de limite (exportação e repositório público) continuem válidos.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,  # noqa: F405
    "DEFAULT_THROTTLE_RATES": {
        **REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"],  # noqa: F405
        "session": "100000/min",
    },
}
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
RABBITMQ_URL = ""
_E2E_FRONTEND_PORT = os.environ.get("E2E_FRONTEND_PORT", "3001")
CORS_ALLOWED_ORIGINS = [f"http://localhost:{_E2E_FRONTEND_PORT}", f"http://127.0.0.1:{_E2E_FRONTEND_PORT}"]
CSRF_TRUSTED_ORIGINS = list(CORS_ALLOWED_ORIGINS)
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
MEDIA_ROOT = _E2E_MEDIA_ROOT
MEDIA_ROOT.mkdir(parents=True, exist_ok=True)

# O worker de extração roda em processo separado com cache em memória; o marker
# de upload pode ficar órfão. O ambiente E2E não precisa dessa proteção.
UPLOAD_TRACKING_ENABLED = False
