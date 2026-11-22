"""
Domain services for authentication, roles, invitations and auditing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import timedelta
from typing import Iterable
from urllib.parse import urlsplit

from allauth.socialaccount.models import SocialAccount
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import HttpRequest
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.models import (
    AuditAction,
    AuditLog,
    ExternalInvitation,
    InvitationStatus,
    NetworkOrigin,
    ProfileRole,
    SessionAudit,
    User,
    UserProfileRole,
)
from accounts.network import (
    classify_ip,
    get_client_ip,
    request_origin,
)

logger = logging.getLogger(__name__)

ROLE_PRIORITY = (
    ProfileRole.ADMIN,
    ProfileRole.COORDINATOR,
    ProfileRole.ADVISOR,
    ProfileRole.STUDENT,
)
TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
TOKEN_PREFIX = "UFVJM"
TOKEN_GROUPS = 4
TOKEN_GROUP_SIZE = 4


class ExternalTokenError(Exception):
    """
    Raised when an external invitation token cannot be exchanged.
    """
class LastAdminError(Exception):
    """
    Raised when an operation would leave the system without an admin.
    """
class UserAlreadyExistsError(Exception):
    """
    Raised when an institutional e-mail is already registered.
    """
def role_label(role: str | None) -> str:
    """
    Return the Portuguese label for a role code.
    """
    if not role:
        return ""
    try:
        return ProfileRole(role).label
    except ValueError:
        return role


def normalize_role(value: str | None) -> str | None:
    """
    Accept either a role code or its label and return the code.
    """
    if not value:
        return None
    candidate = str(value).strip()
    if candidate.upper() in ProfileRole.values:
        return candidate.upper()
    for role in ProfileRole:
        if role.label.lower() == candidate.lower():
            return role.value
    return None


def active_role_codes(user: User) -> list[str]:
    """
    Return active role codes ordered by business priority.
    """
    assigned = set(
        user.active_profile_roles().values_list("role", flat=True)
    )
    return [role for role in ROLE_PRIORITY if role in assigned]


def get_active_role(user: User) -> str | None:
    """
    Return the highest-priority role currently assigned in the database.
    """
    roles = active_role_codes(user)
    if not roles:
        return None
    return roles[0]


def _initials(name: str, email: str) -> str:
    parts = [part for part in name.split() if part]
    if parts:
        return "".join(part[0].upper() for part in parts[:2])
    return email[:2].upper()


def _avatar_url(social_account: SocialAccount | None) -> str | None:
    """
    Return a safe profile picture URL stored by the social provider.
    """
    if social_account is None:
        return None
    candidate = (social_account.extra_data or {}).get("picture")
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return candidate


GOOGLE_SOCIAL_ACCOUNTS_ATTR = "google_social_accounts"


def google_social_accounts_prefetch(lookup: str = "socialaccount_set") -> Prefetch:
    """
    Build the prefetch of a user's Google accounts used by ``user_avatar_url``.

    Args:
        lookup: Path to the users' ``socialaccount_set`` (e.g.
            ``"members__institutional_member__socialaccount_set"``).

    Returns:
        Prefetch: Prefetch storing the Google accounts in ``user.google_social_accounts``.
    """
    google_accounts = SocialAccount.objects.filter(provider="google")
    return Prefetch(lookup, queryset=google_accounts, to_attr=GOOGLE_SOCIAL_ACCOUNTS_ATTR)


def user_avatar_url(user: User) -> str | None:
    """
    Return the user's Google profile picture URL, reusing ``_avatar_url`` rules.

    Uses ``google_social_accounts`` when prefetched (see
    ``google_social_accounts_prefetch``) and queries the account otherwise.

    Args:
        user: Institutional user.

    Returns:
        str | None: Safe https picture URL, or None when there is no Google account or picture.
    """
    prefetched_accounts = getattr(user, GOOGLE_SOCIAL_ACCOUNTS_ATTR, None)
    if prefetched_accounts is not None:
        google_account = prefetched_accounts[0] if prefetched_accounts else None
    else:
        google_account = user.socialaccount_set.filter(provider="google").first()
    return _avatar_url(google_account)


def build_user_payload(user: User, active_role: str | None = None, last_access: timezone.datetime | None = None, avatar_url: str | None = None, roles: list[str] | None = None,) -> dict:
    """
    Serialize the public user shape expected by the frontend.

    Args:
        user: Authenticated user instance.
        active_role: Effective role code, when already resolved.
        last_access: Last recorded activity timestamp.
        avatar_url: Optional profile image URL.
        roles: Active role codes, when already resolved.

    Returns:
        dict: Serialized user payload.
    """
    resolved_roles = roles if roles is not None else active_role_codes(user)
    role = active_role or get_active_role(user)
    return {
        "id": str(user.pk),
        "name": user.name or user.email,
        "email": user.email,
        "affiliation": "",
        "status": "Ativo" if user.is_active else "Inativo",
        "initials": _initials(user.name, user.email),
        "avatarUrl": avatar_url,
        "course": None,
        "lastAccess": last_access.isoformat() if last_access else None,
        "roleCode": role,
        "roleLabel": role_label(role),
        "roles": [{"code": code, "label": role_label(code)} for code in resolved_roles],
    }


def build_session_payload(request: HttpRequest) -> dict:
    """
    Build the authenticated session contract for the frontend.

    Args:
        request: Current HTTP request.

    Returns:
        dict: Session payload consumed by the frontend.
    """
    from accounts.authentication import ExternalInvitationPrincipal

    if isinstance(request.user, ExternalInvitationPrincipal):
        invitation = request.external_invitation
        return {
            "authenticated": True,
            "authType": "external",
            "user": {
                "id": str(invitation.pk),
                "name": invitation.name,
                "email": invitation.email,
                "affiliation": invitation.institution,
                "status": "Ativo",
                "initials": _initials(invitation.name, invitation.email),
                "avatarUrl": None,
                "course": None,
                "lastAccess": None,
                "roleCode": "EXTERNAL",
                "roleLabel": "Membro externo",
                "roles": [],
            },
            "externalInvitation": {
                "id": str(invitation.pk),
                "name": invitation.name,
                "email": invitation.email,
                "institution": invitation.institution,
                "expiresAt": invitation.expires_at.isoformat(),
                "assignments": external_invitation_assignments(invitation),
            },
        }

    user = request.user
    if not user or not user.is_authenticated:
        return {
            "authenticated": False,
            "authType": None,
            "user": None,
            "externalInvitation": None,
        }

    roles = active_role_codes(user)
    active_role = get_active_role(user)
    last_access = (
        user.session_audits.order_by("-last_activity_at")
        .values_list("last_activity_at", flat=True)
        .first()
    )
    google_account = user.socialaccount_set.filter(provider="google").first()
    auth_type = "google" if google_account else "institutional"
    avatar_url = _avatar_url(google_account)
    return {
        "authenticated": True,
        "authType": auth_type,
        "user": build_user_payload(user, active_role, last_access, avatar_url, roles),
        "externalInvitation": None,
    }


def _audit_payload(*, action: str, entity_type: str, entity_id: str, entity_app_label: str, actor_user_id: str | None, actor_external_invitation_id: str | None, actor_role: str | None, actor_roles: list[str], category: str, origin: str, ip_address: str | None, user_agent: str, changes: dict, metadata: dict, occurred_at: timezone.datetime, previous_hash_hex: str | None,) -> dict:
    """
    Build the canonical payload used to compute the audit row hash.

    ``actor_roles`` is only added when the actor actually holds roles. Records
    created before the field existed (and system/token sessions without roles)
    keep the original payload shape, so the hash chain of historical rows can
    still be verified.

    Args:
        action: Audit action code.
        entity_type: Affected entity type.
        entity_id: Affected entity identifier.
        entity_app_label: Django app label of the entity.
        actor_user_id: Institutional actor identifier, when present.
        actor_external_invitation_id: External invitation actor identifier, when present.
        actor_role: Primary role code of the actor, when present.
        actor_roles: Every role code held by the actor.
        origin: Network origin of the request.
        ip_address: Client IP address.
        user_agent: Client user agent.
        changes: Field changes recorded by the operation.
        metadata: Extra context recorded by the operation.
        occurred_at: Moment of the operation.
        previous_hash_hex: Hexadecimal hash of the previous row.

    Returns:
        dict: Payload with the canonical fields used by the hash.
    """
    payload = {
        "actor_user_id": actor_user_id,
        "actor_external_invitation_id": actor_external_invitation_id,
        "actor_role": actor_role,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_app_label": entity_app_label,
        "origin": origin,
        "ip_address": ip_address,
        "user_agent": user_agent,
        "changes": changes,
        "metadata": metadata,
        "occurred_at": occurred_at.isoformat(),
        "previous_hash": previous_hash_hex,
    }
    if actor_roles:
        payload["actor_roles"] = actor_roles
    if category:
        payload["category"] = category
    return payload


def record_audit(*, action: str, entity_type: str, entity_id: str, request: HttpRequest | None = None, actor_user: User | None = None, actor_external_invitation: ExternalInvitation | None = None, actor_role: str | None = None, actor_roles: Iterable[str] | None = None, origin: str | None = None, ip_address: str | None = None, user_agent: str | None = None, changes: dict | None = None, metadata: dict | None = None, entity_app_label: str = "accounts",) -> AuditLog:
    """
    Append a hash-chained audit entry.
    """
    occurred_at = timezone.now()
    changes = changes or {}
    metadata = metadata or {}
    from accounts.audit_presentation import derive_audit_category
    computed_category = derive_audit_category(str(action), entity_type, metadata)

    if actor_user is None and request is not None:
        candidate = getattr(request, "user", None)
        if candidate is not None and candidate.is_authenticated and isinstance(candidate, User):
            actor_user = candidate

    if actor_role is None and actor_user is not None:
        actor_role = get_active_role(actor_user)

    if actor_roles is None and actor_user is not None:
        actor_roles = active_role_codes(actor_user)
    if actor_roles is None:
        actor_roles = [actor_role] if actor_role else []
    normalized_actor_roles: list[str] = []
    for role in actor_roles:
        role_code = str(role)
        if role_code and role_code not in normalized_actor_roles:
            normalized_actor_roles.append(role_code)

    if request is not None:
        ip_address = ip_address or get_client_ip(request)
        user_agent = user_agent or request.META.get("HTTP_USER_AGENT", "")
        origin = origin or request_origin(request)
    else:
        origin = origin or classify_ip(ip_address)
    user_agent = (user_agent or "")[:512]
    origin = str(origin)

    with transaction.atomic():
        previous = (
            AuditLog.objects.select_for_update()
            .order_by("-occurred_at", "-id")
            .first()
        )
        previous_hash = previous.row_hash if previous else None
        previous_hash_hex = (
            bytes(previous_hash).hex() if previous_hash is not None else None
        )
        payload = _audit_payload(
            action=str(action),
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_app_label=entity_app_label,
            actor_user_id=str(actor_user.pk) if actor_user else None,
            actor_external_invitation_id=(
                str(actor_external_invitation.pk)
                if actor_external_invitation
                else None
            ),
            actor_role=str(actor_role) if actor_role else None,
            actor_roles=normalized_actor_roles,
            category=computed_category,
            origin=origin,
            ip_address=ip_address,
            user_agent=user_agent,
            changes=changes,
            metadata=metadata,
            occurred_at=occurred_at,
            previous_hash_hex=previous_hash_hex,
        )
        row_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).digest()

        return AuditLog.objects.create(
            actor_user=actor_user,
            actor_external_invitation=actor_external_invitation,
            actor_role=actor_role,
            actor_roles=normalized_actor_roles,
            category=computed_category,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_app_label=entity_app_label,
            origin=origin,
            ip_address=ip_address,
            user_agent=user_agent,
            changes=changes,
            metadata=metadata,
            previous_hash=previous_hash,
            row_hash=row_hash,
            occurred_at=occurred_at,
        )


def count_active_admins(exclude_user: User | None = None) -> int:
    """
    Count active users that still hold administrative capabilities.
    """
    queryset = User.objects.filter(is_active=True).filter(
        Q(is_staff=True)
        | Q(is_superuser=True)
        | Q(
            profile_roles__role=ProfileRole.ADMIN,
            profile_roles__revoked_at__isnull=True,
        )
    )
    if exclude_user is not None:
        queryset = queryset.exclude(pk=exclude_user.pk)
    return queryset.distinct().count()


def _is_admin_user(user: User) -> bool:
    return bool(
        user.is_staff
        or user.is_superuser
        or user.has_active_role(ProfileRole.ADMIN)
    )


def set_user_roles(user: User, roles: Iterable[str], assigned_by: User | None = None,) -> list[str]:
    """
    Replace the active roles of a user while preserving history.
    """
    normalized_roles = []
    for role in roles:
        normalized = normalize_role(role)
        if not normalized:
            raise ValidationError("Perfil inválido.")
        if normalized not in normalized_roles:
            normalized_roles.append(normalized)
    if not normalized_roles:
        raise ValidationError("Informe ao menos um perfil.")
    if ProfileRole.STUDENT in normalized_roles and len(normalized_roles) > 1:
        raise ValidationError("O perfil Aluno não pode ser combinado com outros perfis.")

    if (
        _is_admin_user(user)
        and ProfileRole.ADMIN not in normalized_roles
        and count_active_admins(exclude_user=user) == 0
    ):
        raise LastAdminError("Não é possível remover o último administrador.")

    current = set(user.active_profile_roles().values_list("role", flat=True))
    requested = set(normalized_roles)
    now = timezone.now()

    user.profile_roles.filter(
        role__in=current - requested,
        revoked_at__isnull=True,
    ).update(revoked_at=now)

    for role in requested - current:
        UserProfileRole.objects.create(
            user=user,
            role=role,
            assigned_by=assigned_by,
        )

    if ProfileRole.ADMIN in requested and not user.is_staff:
        user.is_staff = True
        user.save(update_fields=["is_staff"])
    elif (
        ProfileRole.ADMIN not in requested
        and user.is_staff
        and not user.is_superuser
    ):
        user.is_staff = False
        user.save(update_fields=["is_staff"])

    return normalized_roles


def create_institutional_user(*, email: str, name: str, roles: Iterable[str], assigned_by: User | None = None, request: HttpRequest | None = None,) -> User:
    """
    Pre-provision an institutional user without a local password.
    """
    normalized_email = (email or "").strip().lower()
    validate_email(normalized_email)
    domain = getattr(settings, "INSTITUTIONAL_EMAIL_DOMAIN", "ufvjm.edu.br")
    if normalized_email.rsplit("@", 1)[-1] != domain:
        raise ValidationError("Informe um e-mail institucional.")
    if User.objects.filter(email__iexact=normalized_email).exists():
        raise UserAlreadyExistsError("Já existe um usuário com este e-mail.")

    with transaction.atomic():
        user = User.objects.create_user(email=normalized_email, name=name.strip())
        set_user_roles(user, roles, assigned_by=assigned_by)
        # Resolve coordenações pendentes vinculadas ao e-mail institucional.
        try:
            from academic.services import resolve_pending_coordinators_for_user

            resolve_pending_coordinators_for_user(user, request=request)
        except Exception:
            logger.exception(
                "Failed to resolve pending coordinators: user_id=%s", user.pk
            )
        record_audit(
            action=AuditAction.CREATE,
            entity_type="User",
            entity_id=str(user.pk),
            entity_app_label="accounts",
            request=request,
            actor_user=assigned_by,
            changes={"name": user.name, "email": user.email, "roles": list(roles)},
            metadata={"name": user.name, "email": user.email},
        )
    logger.info("Institutional user created: user_id=%s", user.pk)
    return user


def deactivate_user(user: User, *, actor: User | None = None, request: HttpRequest | None = None,) -> User:
    """
    Soft-delete a user, protecting the last administrative account.
    """
    if _is_admin_user(user) and count_active_admins(exclude_user=user) == 0:
        raise LastAdminError("Não é possível desativar o último administrador.")
    if actor is not None and actor.pk == user.pk:
        raise ValidationError("O administrador não pode desativar a própria conta.")

    with transaction.atomic():
        User.objects.deactivate(user)
        record_audit(
            action=AuditAction.DELETE,
            entity_type="User",
            entity_id=str(user.pk),
            entity_app_label="accounts",
            request=request,
            actor_user=actor,
            changes={"is_active": False},
            metadata={"name": user.name, "email": user.email},
        )
    logger.info("User deactivated: user_id=%s", user.pk)
    return user


def reactivate_user(user: User, roles: Iterable[str] | None = None, *, actor: User | None = None, request: HttpRequest | None = None,) -> User:
    """
    Reactivate a previously soft-deleted user.
    """
    with transaction.atomic():
        if not user.is_active:
            user.is_active = True
            user.save(update_fields=["is_active"])
        if roles is not None:
            set_user_roles(user, roles, assigned_by=actor)
        record_audit(
            action=AuditAction.UPDATE,
            entity_type="User",
            entity_id=str(user.pk),
            entity_app_label="accounts",
            request=request,
            actor_user=actor,
            changes={"is_active": True},
            metadata={"name": user.name, "email": user.email},
        )
    return user


def _normalize_external_token(token: str) -> str:
    return (token or "").strip().upper()


def hash_external_token(token: str) -> bytes:
    """
    Return the SHA-256 digest stored for an external invitation token.
    """
    return hashlib.sha256(_normalize_external_token(token).encode("utf-8")).digest()


def generate_external_token() -> str:
    """
    Generate a human-friendly high-entropy external access token.
    """
    groups = [
        "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(TOKEN_GROUP_SIZE))
        for _ in range(TOKEN_GROUPS)
    ]
    return f"{TOKEN_PREFIX}-{'-'.join(groups)}"


def external_invitation_assignments(invitation: ExternalInvitation) -> list[dict[str, str]]:
    """
    Return the defense committee memberships bound to an external invitation.

    Uses the reverse one-to-one ``defense_membership`` (declared by
    ``theses.DefenseMember``) so the accounts app does not import the theses app.

    Args:
        invitation: External invitation.

    Returns:
        list[dict[str, str]]: ``[{"defenseId", "memberId"}]``; empty when the invitation is not on a committee.
    """
    try:
        membership = invitation.defense_membership
    except ObjectDoesNotExist:
        return []
    return [{"defenseId": str(membership.defense_id), "memberId": str(membership.pk)}]


def create_external_invitation(*, name: str, email: str, institution: str = "", invited_by: User | None = None, expires_at: timezone.datetime | None = None, request: HttpRequest | None = None,) -> tuple[ExternalInvitation, str]:
    """
    Create an external invitation and return its one-time readable token.
    """
    normalized_email = (email or "").strip().lower()
    validate_email(normalized_email)
    token = generate_external_token()
    if expires_at is None:
        expires_at = timezone.now() + timedelta(
            hours=getattr(settings, "EXTERNAL_INVITATION_TOKEN_TTL_HOURS", 72)
        )

    with transaction.atomic():
        invitation = ExternalInvitation.objects.create(
            invited_by=invited_by,
            name=name.strip(),
            email=normalized_email,
            institution=(institution or "").strip(),
            token_hash=hash_external_token(token),
            expires_at=expires_at,
        )
        record_audit(
            action=AuditAction.INVITE,
            entity_type="ExternalInvitation",
            entity_id=str(invitation.pk),
            entity_app_label="accounts",
            request=request,
            actor_user=invited_by,
            metadata={"name": invitation.name, "email": invitation.email, "institution": invitation.institution},
        )
    logger.info("External invitation created: invitation_id=%s", invitation.pk)
    return invitation, token


def exchange_external_token(token: str, request: HttpRequest) -> ExternalInvitation:
    """
    Exchange a valid invitation token for a temporary external session.
    """
    digest = hash_external_token(token)
    now = timezone.now()

    with transaction.atomic():
        try:
            invitation = ExternalInvitation.objects.select_for_update().get(
                token_hash=digest
            )
        except ExternalInvitation.DoesNotExist as exc:
            logger.warning("External token exchange failed: unknown token.")
            raise ExternalTokenError("Token inválido ou expirado.") from exc

        if invitation.revoked_at is not None or invitation.status in {
            InvitationStatus.REVOKED,
            InvitationStatus.USED,
            InvitationStatus.DECLINED,
            InvitationStatus.EXPIRED,
        }:
            logger.warning(
                "External token exchange rejected: invitation_id=%s status=%s",
                invitation.pk,
                invitation.status,
            )
            raise ExternalTokenError("Token inválido ou expirado.")

        if invitation.expires_at <= now:
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            raise ExternalTokenError("Token inválido ou expirado.")

        if invitation.accepted_at is None:
            invitation.accepted_at = now
        invitation.used_at = now
        invitation.status = InvitationStatus.USED
        invitation.save(
            update_fields=["accepted_at", "used_at", "status"]
        )

        ttl_minutes = getattr(settings, "EXTERNAL_SESSION_TTL_MINUTES", 240)
        session_expires_at = min(
            now + timedelta(minutes=ttl_minutes),
            invitation.expires_at,
        )
        request.session.flush()
        request.session["external_invitation_id"] = str(invitation.pk)
        request.session["external_session_expires_at"] = (
            session_expires_at.isoformat()
        )
        request.session.set_expiry(
            max(1, int((session_expires_at - now).total_seconds()))
        )

        record_audit(
            action=AuditAction.TOKEN_ACCESS,
            entity_type="ExternalInvitation",
            entity_id=str(invitation.pk),
            entity_app_label="accounts",
            request=request,
            actor_external_invitation=invitation,
            metadata={"session_expires_at": session_expires_at.isoformat()},
        )

    logger.info("External token exchanged: invitation_id=%s", invitation.pk)
    return invitation


def revoke_external_invitation(invitation: ExternalInvitation, *, revoked_by: User | None = None, request: HttpRequest | None = None,) -> ExternalInvitation:
    """
    Revoke an invitation, preventing further token exchanges.
    """
    if invitation.revoked_at is None:
        invitation.revoked_at = timezone.now()
    invitation.status = InvitationStatus.REVOKED
    invitation.revoked_by = revoked_by
    invitation.save(update_fields=["revoked_at", "status", "revoked_by"])
    record_audit(
        action=AuditAction.DELETE,
        entity_type="ExternalInvitation",
        entity_id=str(invitation.pk),
        entity_app_label="accounts",
        request=request,
        actor_user=revoked_by,
        changes={"status": InvitationStatus.REVOKED},
        metadata={"name": invitation.name, "email": invitation.email},
    )
    return invitation


def external_session_expiration(request: HttpRequest) -> timezone.datetime | None:
    """
    Return the expiration timestamp stored in the external session.
    """
    raw_value = request.session.get("external_session_expires_at")
    if not raw_value:
        return None
    raw_value_str = str(raw_value)
    return parse_datetime(raw_value_str)


def origin_for_session(request: HttpRequest) -> NetworkOrigin:
    """
    Return and cache the request network origin.
    """
    origin = request_origin(request)
    request.network_origin = origin
    return origin


def session_audit_for_request(request: HttpRequest, user: User,) -> SessionAudit | None:
    """
    Return the current session audit row, if any.
    """
    audit_id = request.session.get("session_audit_id")
    if audit_id:
        return SessionAudit.objects.filter(pk=audit_id, user=user).first()
    session_key = request.session.session_key
    if not session_key:
        return None
    return SessionAudit.objects.filter(session_key=session_key, user=user).first()


# TTL curto: usuários institucionais mudam muito raramente e a busca deve ser
# rápida mesmo com várias digitações por segundo.
USER_SUGGEST_CACHE_TTL_SECONDS = 45
USER_SUGGEST_MAX_RESULTS = 3
USER_SUGGEST_MIN_QUERY = 2
USER_SUGGEST_MAX_QUERY = 80


def suggest_users(query: str, role_codes: tuple[ProfileRole, ...] | None = None) -> list[dict[str, object]]:
    """
    Return up to three institutional users matching a search fragment.

    Powers the member-selection autocomplete. The result is cached briefly and
    capped at a handful of rows, so the endpoint stays cheap even with frequent
    calls (the frontend debounces too). Name/email are stable enough that a
    45-second staleness window is acceptable.

    Args:
        query: Free text typed by the user (at least ``USER_SUGGEST_MIN_QUERY``
            characters; anything longer than ``USER_SUGGEST_MAX_QUERY`` is ignored).
        role_codes: Restrict matches to users that hold one of these roles
            (advisors calling the member picker only see other advisors;
            administrators and coordinators see the full directory).

    Returns:
        list[dict[str, object]]: Matches with ``id``, ``name``, ``email`` and
            ``avatarUrl``, ordered by name then e-mail.
    """
    normalized = query.strip().lower()
    if len(normalized) < USER_SUGGEST_MIN_QUERY or len(normalized) > USER_SUGGEST_MAX_QUERY:
        return []
    roles_key = ",".join(sorted(role_codes)) if role_codes else "__all__"
    cache_key = f"users:suggest:{roles_key}:{normalized}"
    cached_results = cache.get(cache_key)
    if cached_results is not None:
        return cached_results
    # `icontains` em nome não usa índice; o volume de usuários institucionais
    # (alguns milhares) torna o seq scan mais barato que manter pg_trgm. Se o
    # volume crescer muito, habilite a extensão e um índice GIN parcial.
    base = User.objects.filter(is_active=True)
    if role_codes:
        base = base.filter(profile_roles__role__in=role_codes, profile_roles__revoked_at__isnull=True)
    matches = (
        base.filter(Q(name__icontains=normalized) | Q(email__startswith=normalized))
        .distinct()
        .only("id", "name", "email")
        .order_by("name", "email")
        .prefetch_related(google_social_accounts_prefetch())[:USER_SUGGEST_MAX_RESULTS]
    )
    results: list[dict[str, object]] = []
    for user in matches:
        results.append(
            {
                "id": str(user.pk),
                "name": user.name or user.email,
                "email": user.email,
                "avatarUrl": user_avatar_url(user),
            }
        )
    cache.set(cache_key, results, USER_SUGGEST_CACHE_TTL_SECONDS)
    return results
