"""
Django settings for thesismanagerbackend project.

The project runs on Django 6.1 (see requirements.txt).

For more information on this file, see
https://docs.djangoproject.com/en/6.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

from pathlib import Path

import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)

env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("DJANGO_SECRET_KEY")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS",
    default=["localhost", "127.0.0.1", "[::1]"],
)

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.sites",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.google",
    "allauth.headless",
    "corsheaders",
    "rest_framework",
    "accounts",
    "academic",
    "theses",
    "reports",
    "events",
    "notifications",
    "indexing",
    "adjudication",
    "repository_readmodel",
    "reports_readmodel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "accounts.middleware.AdminNetworkRestrictionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "accounts.middleware.SessionActivityMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

SERVICE_NAME = env("SERVICE_NAME", default="")
if SERVICE_NAME:
    ROOT_URLCONF = f"thesismanagerbackend.service_urls.{SERVICE_NAME}"
else:
    ROOT_URLCONF = "thesismanagerbackend.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "thesismanagerbackend.wsgi.application"


# Database
# https://docs.djangoproject.com/en/4.2/ref/settings/#databases

DATABASE_URL = env("DATABASE_URL")
DATABASES = {
    "default": env.db("DATABASE_URL"),
}

# Cada serviço possui seu próprio banco em produção. O fallback mantém um
# único banco em desenvolvimento/teste, onde não há dados reais a preservar.
SERVICE_DATABASE_ALIASES = ("auth", "committee", "repository", "reports", "notifications")
# O banco de committee está reservado para o futuro serviço de bancas; a lógica
# atual vive no app theses. Fica listado aqui para não ser tratado como órfão.
RESERVED_DATABASE_ALIASES = ("committee",)
for _alias in SERVICE_DATABASE_ALIASES:
    _url = env(f"{_alias.upper()}_DATABASE_URL", default=DATABASE_URL)
    DATABASES[_alias] = env.db_url_config(_url)

DATABASE_ROUTERS = ["thesismanagerbackend.routers.ServiceDatabaseRouter"]
SERVICE_DATABASES_SHARED = env.bool("SERVICE_DATABASES_SHARED", default=True)
SERVICE_TOKEN = env("SERVICE_TOKEN", default="local-service-token-change-before-deploy")


# Password validation
# https://docs.djangoproject.com/en/4.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Autenticação e django-allauth
# https://docs.djangoproject.com/en/4.2/topics/auth/
# https://docs.allauth.org/

AUTH_USER_MODEL = "accounts.User"
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:3000")
LOGIN_REDIRECT_URL = FRONTEND_URL
LOGOUT_REDIRECT_URL = FRONTEND_URL

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SITE_ID = 1

ACCOUNT_ADAPTER = "accounts.adapters.InstitutionalAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_USER_MODEL_EMAIL_FIELD = "email"
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_RATE_LIMITS = {
    "login_failed": "5/5m",
    "signup": "10/h",
    "reset_password": "5/h",
}

SOCIALACCOUNT_ADAPTER = "accounts.adapters.InstitutionalSocialAccountAdapter"
SOCIALACCOUNT_ONLY = True
SOCIALACCOUNT_QUERY_EMAIL = True
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION = True
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = True

INSTITUTIONAL_EMAIL_DOMAIN = env(
    "INSTITUTIONAL_EMAIL_DOMAIN", default="ufvjm.edu.br"
).lower()
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", default="")
GOOGLE_CLIENT_SECRET = env("GOOGLE_CLIENT_SECRET", default="")

SOCIALACCOUNT_PROVIDERS: dict = {}
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    SOCIALACCOUNT_PROVIDERS["google"] = {
        "APP": {
            "client_id": GOOGLE_CLIENT_ID,
            "secret": GOOGLE_CLIENT_SECRET,
            "key": "",
        },
        "SCOPE": ["profile", "email"],
        "AUTH_PARAMS": {
            "access_type": "online",
            "hd": INSTITUTIONAL_EMAIL_DOMAIN,
            "prompt": "select_account",
        },
    }

HEADLESS_ONLY = True
HEADLESS_CLIENTS = ("browser",)
HEADLESS_FRONTEND_URLS = {
    "socialaccount_login_error": f"{FRONTEND_URL}/login?erro=social",
}

ALLAUTH_TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)


# Django REST Framework
# https://www.django-rest-framework.org/api-guide/settings/

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.ExternalInvitationSessionAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "accounts.permissions.IsInstitutionalUser",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_THROTTLE_CLASSES": [
        # Mesma regra de IP de accounts.network: X-Forwarded-For só vale quando o
        # par é um proxy confiável (TRUSTED_PROXY_CIDRS), ao contrário de NUM_PROXIES.
        "thesismanagerbackend.throttling.ClientIpScopedRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "external_token": "10/min",
        # Reenvio e novo convite de membro externo: cada pedido dispara um e-mail com token novo.
        "invitation_resend": "5/min",
        "public_download": "30/min",
        "public_repository": "120/min",
        "reports": "60/min",
        "reports_export": "10/min",
        "session": "120/min",
        "thesis": "120/min",
        "thesis_upload": "30/hour",
        "upload_global": "50/day",
        "user_suggest": "60/min",
    },
}


# Frontend e CORS
# https://github.com/adamchainz/django-cors-headers

CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=[FRONTEND_URL],
)
CORS_ALLOW_CREDENTIALS = True
# Sem isso o navegador esconde o nome do arquivo dos downloads (PDF de relatórios,
# CSV de auditoria) do frontend, que roda em outra origem.
CORS_EXPOSE_HEADERS = ["Content-Disposition"]
CSRF_TRUSTED_ORIGINS = env.list(
    "CSRF_TRUSTED_ORIGINS",
    default=[FRONTEND_URL],
)


# Cache Redis e sessões
# https://docs.djangoproject.com/en/4.2/topics/cache/

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "thesis_manager",
        "TIMEOUT": 300,
    }
}

SESSION_DB_ALIAS = "auth" if env("SERVICE_DATABASES_SHARED", default="True").lower() in {"0", "false", "no"} else "default"
SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", default=8 * 60 * 60)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = env("SESSION_COOKIE_SAMESITE", default="Lax")
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_HTTPONLY = False
CSRF_COOKIE_SAMESITE = env("CSRF_COOKIE_SAMESITE", default="Lax")
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)


# Configuração de redes confiáveis
# Só considere cabeçalhos de encaminhamento quando a quantidade de proxies for configurada.

TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)
TRUSTED_PROXY_CIDRS = env.list("TRUSTED_PROXY_CIDRS", default=[])
INSTITUTIONAL_NETWORK_CIDRS = env.list("INSTITUTIONAL_NETWORK_CIDRS", default=[])
VPN_NETWORK_CIDRS = env.list("VPN_NETWORK_CIDRS", default=[])
ADMIN_ALLOWED_CIDRS = env.list(
    "ADMIN_ALLOWED_CIDRS",
    default=["127.0.0.1/32", "::1/128"] if DEBUG else [],
)
ADMIN_URL_PREFIX = "admin/"
SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS = env.int(
    "SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS", default=300
)


# Convites externos para membros de banca
EXTERNAL_INVITATION_TOKEN_TTL_HOURS = env.int(
    "EXTERNAL_INVITATION_TOKEN_TTL_HOURS", default=72
)
EXTERNAL_SESSION_TTL_MINUTES = env.int("EXTERNAL_SESSION_TTL_MINUTES", default=240)


# Email
# Padrão SMTP, e não console: o backend de console imprimiria no log o token de acesso
# enviado aos membros externos da banca (ver notifications.checks).
EMAIL_BACKEND = env(
    "EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend"
)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="nao-responda@ufvjm.edu.br")
EMAIL_HOST = env("EMAIL_HOST", default="")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)


# Eventos assíncronos (transactional outbox + RabbitMQ)
# A URL do broker contém credenciais: nunca registrar em log nem em `last_error`.
RABBITMQ_URL = env("RABBITMQ_URL", default="")
EVENTS_EXCHANGE = env("EVENTS_EXCHANGE", default="thesis.events")
OUTBOX_BATCH_SIZE = env.int("OUTBOX_BATCH_SIZE", default=50)
OUTBOX_POLL_INTERVAL_SECONDS = env.float("OUTBOX_POLL_INTERVAL_SECONDS", default=2.0)
OUTBOX_MAX_ATTEMPTS = env.int("OUTBOX_MAX_ATTEMPTS", default=10)
OUTBOX_RETRY_BASE_SECONDS = env.int("OUTBOX_RETRY_BASE_SECONDS", default=5)
OUTBOX_RETRY_MAX_SECONDS = env.int("OUTBOX_RETRY_MAX_SECONDS", default=900)
NOTIFICATIONS_QUEUE = env("NOTIFICATIONS_QUEUE", default="notifications.thesis-events")
NOTIFICATIONS_PREFETCH = env.int("NOTIFICATIONS_PREFETCH", default=10)
INDEXING_QUEUE = env("INDEXING_QUEUE", default="indexing.thesis-events")
INDEXING_PREFETCH = env.int("INDEXING_PREFETCH", default=5)
ADJUDICATION_QUEUE = env("ADJUDICATION_QUEUE", default="adjudication.thesis-events")
ADJUDICATION_PREFETCH = env.int("ADJUDICATION_PREFETCH", default=5)
REPOSITORY_QUEUE = env("REPOSITORY_QUEUE", default="repository.thesis-events")
REPOSITORY_PREFETCH = env.int("REPOSITORY_PREFETCH", default=5)
REPORTS_QUEUE = env("REPORTS_QUEUE", default="reports.thesis-events")
REPORTS_PREFETCH = env.int("REPORTS_PREFETCH", default=5)


# Logging
# Os workers de eventos rodam fora do runserver e precisam de saída em stdout.
# Apenas os apps de eventos são configurados para não duplicar os logs do Django.
LOG_LEVEL = env("LOG_LEVEL", default="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "loggers": {
        "events": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "notifications": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}


# Reforço de segurança (habilitar em produção pelas variáveis de ambiente)

SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False
)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# Internationalization
# https://docs.djangoproject.com/en/4.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/4.2/howto/static-files/

STATIC_URL = "static/"

# Uploaded thesis files are private by default and served through permission-checked views.
MEDIA_ROOT = env("MEDIA_ROOT", default=str(BASE_DIR / "media"))
MEDIA_URL = "/media/"

THESIS_MAX_PDF_SIZE_MB = env.int("THESIS_MAX_PDF_SIZE_MB", default=50)
THESIS_MAX_DOCUMENT_SIZE_MB = env.int("THESIS_MAX_DOCUMENT_SIZE_MB", default=20)
THESIS_MAX_KEYWORDS = env.int("THESIS_MAX_KEYWORDS", default=10)
THESIS_MAX_AUTHORS = env.int("THESIS_MAX_AUTHORS", default=5)
UPLOAD_TRACKING_TTL_SECONDS = env.int("UPLOAD_TRACKING_TTL_SECONDS", default=3600)
UPLOAD_GLOBAL_DAILY_LIMIT = env.int("UPLOAD_GLOBAL_DAILY_LIMIT", default=50)
# Cache Redis do repositório público; a invalidação é explícita (publicação,
# edição ou despublicação), então o TTL só limita dados velhos de curso/orientador.
PUBLIC_REPOSITORY_CACHE_TTL_SECONDS = env.int("PUBLIC_REPOSITORY_CACHE_TTL_SECONDS", default=300)

# Default primary key field type
# https://docs.djangoproject.com/en/4.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
