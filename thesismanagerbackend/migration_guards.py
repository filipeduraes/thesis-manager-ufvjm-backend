"""Invariants that must hold for the split-database deployment.

Django happily generates a real ``REFERENCES`` constraint for a foreign key to
another service's table unless ``db_constraint=False`` is set. On PostgreSQL
that migration fails because the target table lives in a different database.
This module inspects on-disk migrations so the invariant can be tested.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations.fields import AddField, AlterField
from django.db.migrations.operations.models import CreateModel

from thesismanagerbackend.routers import SERVICE_APP_ALIASES


@dataclass(frozen=True)
class ConstraintViolation:
    """A cross-service relation that would emit an invalid database constraint."""

    migration: str
    model: str
    field: str
    target: str

    def __str__(self) -> str:
        return f"{self.migration}: {self.model}.{self.field} -> {self.target}"


def alias_for_app(app_label: str) -> str:
    """Return the database alias that owns an app's tables."""
    return SERVICE_APP_ALIASES.get(app_label, "default")


def target_app_label(model_reference: object) -> str:
    """Resolve the app label of a migration's (possibly string) model reference."""
    if isinstance(model_reference, str):
        return model_reference.split(".")[0]
    return model_reference._meta.app_label  # type: ignore[union-attr]


def relation_fields(operation: Any) -> Iterable[tuple[str, Any]]:
    """Yield relation fields declared by a CreateModel/AddField/AlterField operation."""
    if isinstance(operation, CreateModel):
        return [(name, field) for name, field in operation.fields if getattr(field, "is_relation", False)]
    if isinstance(operation, (AddField, AlterField)):
        return [(operation.name, operation.field)] if getattr(operation.field, "is_relation", False) else []
    return []


def violation_for(app_label: str, migration: str, operation: Any) -> ConstraintViolation | None:
    """Return a violation when an operation would constrain a cross-service FK."""
    for field_name, field in relation_fields(operation):
        remote = getattr(field, "remote_field", None)
        if remote is None or not getattr(field, "db_constraint", False):
            continue
        if alias_for_app(app_label) == alias_for_app(target_app_label(remote.model)):
            continue
        return ConstraintViolation(
            migration=migration,
            model=getattr(operation, "name", app_label),
            field=field_name,
            target=str(remote.model),
        )
    return None


def cross_service_constraints() -> list[ConstraintViolation]:
    """Return every cross-service relation with a real database constraint."""
    loader = MigrationLoader(connection)
    violations: list[ConstraintViolation] = []
    for key, migration in loader.disk_migrations.items():
        for operation in migration.operations:
            violation = violation_for(key[0], f"{key[0]}.{key[1]}", operation)
            if violation is not None:
                violations.append(violation)
    return violations


def aliases_without_mapped_app() -> set[str]:
    """Return configured database aliases that no app writes to."""
    mapped = set(SERVICE_APP_ALIASES.values())
    mapped.update(getattr(settings, "RESERVED_DATABASE_ALIASES", ()))
    return set(settings.SERVICE_DATABASE_ALIASES) - mapped
