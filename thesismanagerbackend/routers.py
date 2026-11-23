"""
Database router that gives each service ownership of its own tables.

In development and in the isolated E2E configuration every alias points to the
same database, so the router is transparent. In production each alias has its
own URL and only the owning service writes to its tables.
"""

from __future__ import annotations

from django.conf import settings

SERVICE_APP_ALIASES: dict[str, str] = {
    # Apps de identidade e suas dependências do Django/Allauth precisam
    # compartilhar o mesmo banco, senão FKs como users_user_groups → auth_group
    # referenciam tabelas de outro banco.
    "accounts": "auth",
    "academic": "auth",
    "auth": "auth",
    "contenttypes": "auth",
    "sessions": "auth",
    "sites": "auth",
    "admin": "auth",
    "account": "auth",
    "socialaccount": "auth",
    "notifications": "notifications",
    "reports": "reports",
    "repository": "repository",
    "repository_readmodel": "repository",
    "reports_readmodel": "reports",
}


def _shares_database(alias: str) -> bool:
    """
    Tell whether database splitting is disabled for this environment.

    Django replaces ``NAME`` with a per-alias test database name, so comparing
    names is unreliable; the explicit setting is the source of truth.

    Args:
        alias: Database alias (unused when the flag is global).

    Returns:
        bool: True when every alias points at the same physical database.
    """
    return bool(getattr(settings, "SERVICE_DATABASES_SHARED", True))


class ServiceDatabaseRouter:
    """Route models and migrations to the database that owns their service."""

    def _alias_for(self, app_label: str) -> str:
        """
        Return the alias that owns an app.

        Args:
            app_label: Django app label.

        Returns:
            str: Database alias.
        """
        return SERVICE_APP_ALIASES.get(app_label, "default")

    def db_for_read(self, model, **hints) -> str | None:
        """
        Choose the read database of a model.

        Args:
            model: Model class.
            **hints: Django router hints.

        Returns:
            str | None: Owning alias when databases are actually split.
        """
        if _shares_database(self._alias_for(model._meta.app_label)):
            return None
        return self._alias_for(model._meta.app_label)

    def db_for_write(self, model, **hints) -> str | None:
        """
        Choose the write database of a model.

        Args:
            model: Model class.
            **hints: Django router hints.

        Returns:
            str | None: Owning alias when databases are actually split.
        """
        return self.db_for_read(model, **hints)

    def allow_relation(self, obj1, obj2, **hints) -> bool | None:
        """
        Allow relations only inside the same physical database.

        Args:
            obj1: First model instance.
            obj2: Second model instance.
            **hints: Django router hints.

        Returns:
            bool | None: True/False when split, None for shared databases.
        """
        if _shares_database(obj1._state.db or "default") and _shares_database(obj2._state.db or "default"):
            return None
        return obj1._state.db == obj2._state.db

    def allow_migrate(self, db: str, app_label: str, **hints) -> bool | None:
        """
        Restrict migrations to the owning alias when databases are split.

        Args:
            db: Database alias being migrated.
            app_label: App being migrated.
            **hints: Django router hints.

        Returns:
            bool | None: Router decision.
        """
        target = self._alias_for(app_label)
        if _shares_database(target) or _shares_database(db):
            return None
        return db == target
