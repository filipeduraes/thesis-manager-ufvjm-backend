"""
Authorization classes for administrative API endpoints.
"""
from rest_framework.permissions import BasePermission

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import ProfileRole


class IsInstitutionalUser(BasePermission):
    """Allow authenticated institutional accounts, excluding invitation sessions."""
    message = "Acesso restrito a usuários institucionais."

    def has_permission(self, request, view) -> bool:
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and user.is_active
            and not isinstance(user, ExternalInvitationPrincipal)
        )


def _has_role(user, roles: tuple[str, ...]) -> bool:
    has_active_role = getattr(user, "has_active_role", None)
    if not callable(has_active_role):
        return False
    return any(has_active_role(role) for role in roles)


class IsInstitutionalManager(BasePermission):
    """
    Allow access to staff/manager roles that need to find other institutional users.

    Students and external committee sessions do not get the directory; the
    endpoint returns only active users with minimal fields.
    """
    message = "Acesso restrito a administradores, coordenadores e orientadores."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if isinstance(user, ExternalInvitationPrincipal):
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "is_staff", False)
            or _has_role(user, (ProfileRole.ADMIN, ProfileRole.COORDINATOR, ProfileRole.ADVISOR))
        )


class IsCoordinatorOrAdmin(BasePermission):
    """
    Allow access only to active coordinators and administrators.
    """
    message = "Acesso restrito a coordenadores e administradores."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        if isinstance(user, ExternalInvitationPrincipal):
            return False
        return bool(
            getattr(user, "is_superuser", False)
            or getattr(user, "is_staff", False)
            or _has_role(user, (ProfileRole.ADMIN, ProfileRole.COORDINATOR))
        )


class IsInstitutionalAdmin(BasePermission):
    """
    Allow access only to active users with administrative capability.
    """
    message = "Acesso restrito a administradores."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        return bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or _has_role(user, (ProfileRole.ADMIN,))
        )

