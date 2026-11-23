"""
Test runner aware of the service database split.

Django test classes default to ``databases = {"default"}``. That is correct for
a single shared database, but with ``SERVICE_DATABASES_SHARED=False`` the router
sends writes to the owning service aliases and Django blocks them. This runner
declares every service alias on the base test classes before the suite is built,
so the same tests exercise the real split-PostgreSQL topology.
"""

from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.test.runner import DiscoverRunner

ALL_SERVICE_ALIASES = frozenset({"default", "auth", "committee", "repository", "reports", "notifications"})


class ServiceAwareTestRunner(DiscoverRunner):
    """Allow every service alias in tests when databases are actually split."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        if not getattr(settings, "SERVICE_DATABASES_SHARED", True):
            for test_class in (SimpleTestCase, TransactionTestCase, TestCase):
                test_class.databases = ALL_SERVICE_ALIASES
