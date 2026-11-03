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


class IsInstitutionalAdmin(BasePermission):
    """
    Allow access only to active users with administrative capability.
    """
    message = "Acesso restrito a administradores."

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not user or not user.is_authenticated or not user.is_active:
            return False
        has_active_role = getattr(user, "has_active_role", None)
        return bool(
            getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
            or (callable(has_active_role) and has_active_role(ProfileRole.ADMIN))
        )
