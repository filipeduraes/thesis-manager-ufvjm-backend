"""
Middlewares for administrative network restrictions and session activity.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import SessionAudit
from accounts.network import get_client_ip, is_ip_allowed

logger = logging.getLogger(__name__)


class AdminNetworkRestrictionMiddleware:
    """
    Restrict administrative areas to trusted institutional networks.
    """
    def __init__(self, get_response):
        self.get_response = get_response
        admin_prefix = "/" + str(
            getattr(settings, "ADMIN_URL_PREFIX", "admin/")
        ).strip("/")
        self.admin_prefix = admin_prefix
        self.api_admin_prefix = "/api/v1/admin"

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self._is_protected_path(request.path_info):
            return self.get_response(request)

        client_ip: str | None = get_client_ip(request)
        allowed_cidrs: list[str] = getattr(settings, "ADMIN_ALLOWED_CIDRS", [])
        if not is_ip_allowed(client_ip, allowed_cidrs):
            logger.warning(
                "Administrative access denied: path=%s ip=%s",
                request.path_info,
                client_ip,
            )
            return JsonResponse({"detail": "Not found."}, status=404)

        return self.get_response(request)

    def _is_protected_path(self, path: str) -> bool:
        is_admin = path == self.admin_prefix or path.startswith(
            f"{self.admin_prefix}/"
        )
        is_admin_api = path == self.api_admin_prefix or path.startswith(
            f"{self.api_admin_prefix}/"
        )
        return is_admin or is_admin_api


class SessionActivityMiddleware:
    """
    Periodically refresh the last activity timestamp of a session audit.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        self._update_activity(request)
        return response

    def _update_activity(self, request: HttpRequest) -> None:
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return
        if request.session.get("external_invitation_id"):
            return

        audit_id = request.session.get("session_audit_id")
        if not audit_id:
            session_key: str | None = request.session.session_key
            if not session_key:
                return
            audit = SessionAudit.objects.filter(session_key=session_key, user=user).first()
            if audit is None:
                return
            request.session["session_audit_id"] = str(audit.pk)
        else:
            audit = SessionAudit.objects.filter(pk=audit_id, user=user).first()
            if audit is None:
                return

        interval: int = getattr(settings, "SESSION_ACTIVITY_UPDATE_INTERVAL_SECONDS", 300)
        now = timezone.now()
        last_update_raw: str = request.session.get("session_activity_updated_at", "")
        last_update_str: str = str(last_update_raw)
        last_update = parse_datetime(last_update_str)
        if last_update and (now - last_update).total_seconds() < interval:
            return

        audit.last_activity_at = now
        audit.save(update_fields=["last_activity_at"])
        request.session["session_activity_updated_at"] = now.isoformat()
