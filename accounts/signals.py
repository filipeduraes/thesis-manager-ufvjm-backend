"""
Signal handlers that maintain session and audit records.
"""
from __future__ import annotations

import logging

from django.contrib.auth.signals import (
    user_login_failed,
    user_logged_in,
    user_logged_out,
)
from django.dispatch import receiver
from django.utils import timezone

from accounts.audit_identifiers import EntityType
from accounts.audit_entry import AuditEntry
from accounts.models import AuditAction, SessionAudit
from accounts.network import classify_ip, get_client_ip
from accounts.services import record_audit

logger = logging.getLogger(__name__)


@receiver(user_logged_in)
def handle_user_logged_in(sender, request, user, **kwargs) -> None:
    """
    Create the session audit row and register the login event.
    """
    if request is None:
        return
    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key
    if not session_key:
        return

    client_ip = get_client_ip(request)
    audit, _ = SessionAudit.objects.update_or_create(
        session_key=session_key,
        user=user,
        defaults={
            "ip_address": client_ip,
            "user_agent": request.META.get("HTTP_USER_AGENT", "")[:512],
            "origin": classify_ip(client_ip),
            "login_at": timezone.now(),
            "logout_at": None,
            "last_activity_at": timezone.now(),
        },
    )
    request.session["session_audit_id"] = str(audit.pk)
    record_audit(AuditEntry(
        action=AuditAction.LOGIN,
        entity_type=EntityType.USER,
        entity_id=str(user.pk),
        request=request,
        actor_user=user,
        metadata={"session_key": session_key},
    ))


@receiver(user_logged_out)
def handle_user_logged_out(sender, request, user, **kwargs) -> None:
    """
    Close the session audit row and register the logout event.
    """
    if request is None or user is None:
        return
    session_key = request.session.session_key
    if not session_key:
        return

    SessionAudit.objects.filter(
        session_key=session_key,
        user=user,
        logout_at__isnull=True,
    ).update(logout_at=timezone.now())

    record_audit(AuditEntry(
        action=AuditAction.LOGOUT,
        entity_type=EntityType.USER,
        entity_id=str(user.pk),
        request=request,
        actor_user=user,
        metadata={"session_key": session_key},
    ))


@receiver(user_login_failed)
def handle_user_login_failed(sender, credentials, request, **kwargs) -> None:
    """
    Register failed authentication attempts without storing secrets.
    """
    if request is None:
        return
    attempted_email = str(
        credentials.get("email") or credentials.get("username") or ""
    ).strip().lower()
    record_audit(AuditEntry(
        action=AuditAction.LOGIN_FAILED,
        entity_type=EntityType.USER,
        entity_id=attempted_email or "anonymous",
        request=request,
        metadata={"provider": "credentials"},
    ))
    logger.warning("Authentication failure recorded for identifier=%s", attempted_email)
