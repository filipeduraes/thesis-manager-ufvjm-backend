"""Transaction helpers that respect the service database router."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable, TypeVar

from django.db import transaction

F = TypeVar("F", bound=Callable[..., Any])


def atomic_for_model(model: type) -> Callable[[F], F]:
    """Open the wrapped function in a transaction on the model's owning alias.

    The alias is resolved at call time so the same code works with shared
    databases (tests/development) and with one database per service.
    """

    def decorator(function: F) -> F:
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            from accounts.concurrency import database_for_model

            with transaction.atomic(using=database_for_model(model)):
                return function(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
