"""URL configuration for thesismanagerbackend project."""

import logging

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path

from accounts.views import HealthView

logger = logging.getLogger(__name__)


def _dev_openai_models_probe(request):
    """
    Responde sondagens OpenAI-compatíveis em desenvolvimento sem poluir os logs.

    Args:
        request: Requisição HTTP recebida.

    Returns:
        JsonResponse: Lista vazia no formato esperado por clientes OpenAI.
    """
    logger.debug(
        "Sondagem dev recebida: path=%s remote_addr=%s x_forwarded_for=%s user_agent=%r",
        request.path,
        request.META.get("REMOTE_ADDR"),
        request.META.get("HTTP_X_FORWARDED_FOR", ""),
        request.META.get("HTTP_USER_AGENT", ""),
    )
    return JsonResponse({"object": "list", "data": []})


urlpatterns = [
    path("health", HealthView.as_view(), name="health"),
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("", include("accounts.urls")),
    path("", include("academic.urls")),
    path("", include("theses.urls")),
]

if settings.DEBUG:
    urlpatterns.insert(0, path("v1/models", _dev_openai_models_probe, name="dev_openai_models_probe"))
    urlpatterns.insert(0, path("v1/models/", _dev_openai_models_probe))
