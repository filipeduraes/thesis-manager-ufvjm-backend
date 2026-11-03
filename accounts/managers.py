"""
Custom managers for the accounts application.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from django.contrib.auth.base_user import BaseUserManager
from django.utils import timezone

if TYPE_CHECKING:
    from accounts.models import User


class UserManager(BaseUserManager):
    """
    Manager that normalizes e-mail addresses before persistence.
    """
    use_in_migrations = True

    def _normalize_email(self, email: str | None) -> str:
        if not email:
            raise ValueError("The e-mail address is required.")
        return self.normalize_email(email).strip().lower()

    def create_user(self, email: str, name: str = "", password: str | None = None, **extra_fields,) -> User:
        """
        Create and persist a regular user.
        """
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        user = self.model(
            email=self._normalize_email(email),
            name=name.strip(),
            **extra_fields,
        )
        user.set_password(password) if password else user.set_unusable_password()
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, name: str = "", password: str | None = None, **extra_fields,) -> User:
        """
        Create and persist an administrative superuser.
        """
        if not password:
            raise ValueError("Superusers must have a password.")
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        if not extra_fields["is_staff"]:
            raise ValueError("Superusers must have is_staff=True.")
        if not extra_fields["is_superuser"]:
            raise ValueError("Superusers must have is_superuser=True.")
        return self.create_user(email, name, password, **extra_fields)

    def deactivate(self, user: User) -> User:
        """
        Soft-delete a user and revoke all of their active roles.
        """
        if not user.is_active:
            return user
        user.is_active = False
        user.save(update_fields=["is_active"])
        user.profile_roles.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        return user
