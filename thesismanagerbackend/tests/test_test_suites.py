"""The fast suite must exist, be importable and exclude known slow/flaky modules."""

from __future__ import annotations

from importlib import import_module

from django.test import SimpleTestCase

from thesismanagerbackend.test_suites import FAST_TEST_LABELS

SLOW_ONLY_LABELS = {
    "notifications.tests.test_templates",
    "reports.tests.test_overview_api",
    "reports.tests.test_overview_pdf",
    "theses.tests.test_public_repository",
}


class FastSuiteTests(SimpleTestCase):
    """Guardrails for the curated regression set."""

    def test_labels_are_importable(self) -> None:
        for label in FAST_TEST_LABELS:
            with self.subTest(label=label):
                import_module(label)

    def test_slow_modules_are_excluded(self) -> None:
        self.assertFalse(SLOW_ONLY_LABELS.intersection(FAST_TEST_LABELS))

    def test_critical_areas_are_covered(self) -> None:
        joined = "\n".join(FAST_TEST_LABELS)
        self.assertIn("test_cross_service_assignments", joined)
        self.assertIn("test_cross_database_queries", joined)
        self.assertIn("test_transaction_aliases", joined)
        self.assertIn("test_consumer_alias_order", joined)
        self.assertIn("test_migration_invariants", joined)
        self.assertIn("test_database_router", joined)
        self.assertIn("test_service_apis", joined)
