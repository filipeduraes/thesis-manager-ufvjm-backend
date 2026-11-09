"""
Authorization classes for the thesis API.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import ProfileRole


def _institutional_user(user) -> bool:
    """
    Return whether the request principal is an active institutional user.

    Args:
        user: Request principal.

    Returns:
        bool: ``True`` for active institutional accounts.
    """
    return bool(
        user
        and user.is_authenticated
        and user.is_active
        and not isinstance(user, ExternalInvitationPrincipal)
    )


def _has_role(user, role: str) -> bool:
    """
    Return whether the principal owns an active business role.

    Args:
        user: Request principal.
        role: Role code.

    Returns:
        bool: ``True`` when the role is active.
    """
    checker = getattr(user, "has_active_role", None)
    if not callable(checker):
        return False
    return checker(role)


class IsStudentUser(BasePermission):
    """
    Allow access only to active users with the student role.
    """

    message = "Acesso restrito a alunos."

    def has_permission(self, request, view) -> bool:
        """
        Check the student role.

        Args:
            request: HTTP request.
            view: Target view.

        Returns:
            bool: Whether the request may proceed.
        """
        user = request.user
        return _institutional_user(user) and _has_role(user, ProfileRole.STUDENT)


class IsAdvisorUser(BasePermission):
    """
    Allow access only to active users with the advisor role.
    """

    message = "Acesso restrito a orientadores."

    def has_permission(self, request, view) -> bool:
        """
        Check the advisor role.

        Args:
            request: HTTP request.
            view: Target view.

        Returns:
            bool: Whether the request may proceed.
        """
        user = request.user
        return _institutional_user(user) and _has_role(user, ProfileRole.ADVISOR)


class IsAcademicManager(BasePermission):
    """
    Allow access to administrators and course coordinators.
    """

    message = "Acesso restrito a coordenadores e administradores."

    def has_permission(self, request, view) -> bool:
        """
        Check the administrative or coordinator role.

        Args:
            request: HTTP request.
            view: Target view.

        Returns:
            bool: Whether the request may proceed.
        """
        user = request.user
        if not _institutional_user(user):
            return False
        if getattr(user, "is_superuser", False):
            return True
        return _has_role(user, ProfileRole.ADMIN) or _has_role(user, ProfileRole.COORDINATOR)
