"""
Small concurrency primitives used by data-integrity rules.

Production uses PostgreSQL (READ COMMITTED). Transaction advisory locks serialize
critical sections that cannot be protected by a single row, such as appending to
the audit hash chain or counting active administrators. SQLite (used only by the
isolated E2E environment) has no equivalent, so the helper is a no-op there; the
concurrency tests that depend on real locking run against PostgreSQL.
"""
from django.db import connection

# Stable arbitrary keys (derived from ASCII strings), distinct from each other.
AUDIT_CHAIN_LOCK_KEY = 0x41554449544C4F47  # "AUDITLOG"
ADMIN_ROLE_LOCK_KEY = 0x41444D494E524F4C  # "ADMINROL"
COURSE_ASSIGNMENT_LOCK_KEY = 0x41535349474E4D54  # "ASSIGNMT"
THESIS_DRAFT_LOCK_KEY = 0x54484553495344  # "THESISD"


def database_for_model(model) -> str:
    """
    Return the database alias that owns a model, respecting the service router.

    Args:
        model: Django model class.

    Returns:
        str: Database alias.
    """
    from django.db import router

    return router.db_for_write(model) or "default"


def acquire_transaction_lock(key: int, using: str = "default") -> None:
    """
    Serialize the current critical section within a transaction.

    Must be called inside ``transaction.atomic()``. The lock is released
    automatically on commit or rollback.

    Args:
        key: Stable advisory-lock identifier.
    """
    from django.db import connections

    if connections[using].vendor != "postgresql":
        return
    with connections[using].cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
