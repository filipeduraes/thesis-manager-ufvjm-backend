"""
DRF authentication for temporary external committee sessions.
"""
from __future__ import annotations

from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.authentication import CSRFCheck
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from accounts.models import ExternalInvitation


class ExternalInvitationPrincipal:
    """
    Minimal principal representing an external invitation session.
    """
    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __init__(self, invitation: ExternalInvitation) -> None:
        self.invitation = invitation
        self.pk = invitation.pk
        self.id = invitation.pk
        self.name = invitation.name
        self.email = invitation.email
        self.username = invitation.email
        self.is_staff = False
        self.is_superuser = False

    def __str__(self) -> str:
        return f"ExternalInvitation:{self.pk}"

    def has_perm(self, perm: str, obj: Any = None) -> bool:
        return False

    def has_module_perms(self, app_label: str) -> bool:
        return False

    def get_all_permissions(self, obj: Any = None) -> set[str]:
        return set()


class ExternalInvitationSessionAuthentication(BaseAuthentication):
    """
    Authenticate requests backed by a valid external invitation session.
    """
    def authenticate(self, request):
        django_request = getattr(request, "_request", request)
        session = getattr(django_request, "session", None)
        if session is None:
            return None

        invitation_id = session.get("external_invitation_id")
        if not invitation_id:
            return None

        expires_at_raw = session.get("external_session_expires_at", "")
        expires_at = parse_datetime(str(expires_at_raw))
        if not expires_at or expires_at <= timezone.now():
            session.flush()
            return None

        invitation = ExternalInvitation.objects.filter(pk=invitation_id).first()
        if not invitation or not invitation.has_valid_external_session:
            session.flush()
            return None

        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            self._enforce_csrf(django_request)

        principal = ExternalInvitationPrincipal(invitation)
        django_request.external_invitation = invitation
        request.external_invitation = invitation
        return principal, invitation

    def authenticate_header(self, request) -> str:
        return "Session"

    def _enforce_csrf(self, request) -> None:
        check = CSRFCheck(lambda _request: None)
        check.process_request(request)
        reason = check.process_view(request, None, (), {})
        if reason:
            raise AuthenticationFailed(f"Falha de CSRF: {reason}")
