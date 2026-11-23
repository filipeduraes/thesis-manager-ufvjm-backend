"""Tests of the per-service database router."""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from accounts.models import User
from notifications.models import Notification
from reports_readmodel.models import ReportFact
from repository_readmodel.models import PublicThesis
from thesismanagerbackend.routers import ServiceDatabaseRouter
from theses.models import Defense, Thesis


class SharedDatabaseRouterTests(SimpleTestCase):
    """In shared mode the router is transparent."""

    def test_no_alias_when_databases_are_shared(self) -> None:
        router = ServiceDatabaseRouter()
        self.assertIsNone(router.db_for_read(Thesis))
        self.assertIsNone(router.db_for_write(User))
        self.assertIsNone(router.db_for_read(Notification))


@override_settings(SERVICE_DATABASES_SHARED=False)
class SplitDatabaseRouterTests(SimpleTestCase):
    """When split, each app resolves to the alias that owns it."""

    def test_models_route_to_owning_service(self) -> None:
        router = ServiceDatabaseRouter()
        self.assertEqual(router.db_for_write(User), "auth")
        self.assertEqual(router.db_for_write(Thesis), "default")
        self.assertEqual(router.db_for_write(Notification), "notifications")
        self.assertEqual(router.db_for_write(PublicThesis), "repository")
        self.assertEqual(router.db_for_write(ReportFact), "reports")

    def test_migrations_are_restricted_to_owner(self) -> None:
        router = ServiceDatabaseRouter()
        self.assertTrue(router.allow_migrate("auth", "accounts"))
        self.assertFalse(router.allow_migrate("default", "accounts"))
        self.assertTrue(router.allow_migrate("repository", "repository_readmodel"))
        self.assertFalse(router.allow_migrate("default", "repository_readmodel"))

    def test_relations_are_rejected_across_databases(self) -> None:
        router = ServiceDatabaseRouter()
        first = Defense(thesis_snapshot={})
        first._state.db = "default"
        second = User(email="a@ufvjm.edu.br")
        second._state.db = "auth"
        self.assertFalse(router.allow_relation(first, second))
