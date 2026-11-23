"""Split-database schema invariants.

The guard is exercised both against the real migration graph and against a
synthetic bad operation, so a future refactor cannot silently neuter it.
"""

from __future__ import annotations

from django.db import models
from django.db.migrations.operations.fields import AddField
from django.test import TestCase

from thesismanagerbackend.migration_guards import aliases_without_mapped_app, cross_service_constraints, violation_for


class MigrationInvariantTests(TestCase):
    """Uses the database only to load the migration graph."""

    databases = {"default"}

    """No on-disk migration may emit a cross-service FK constraint."""

    def test_real_migrations_have_no_cross_service_constraints(self) -> None:
        self.assertEqual(cross_service_constraints(), [])

    def test_guard_detects_a_cross_service_constraint(self) -> None:
        field = models.ForeignKey("accounts.User", on_delete=models.CASCADE, db_constraint=True)
        violation = violation_for("theses", "theses.9999_bad", AddField(model_name="thesis", name="advisor", field=field))
        self.assertIsNotNone(violation)
        assert violation is not None
        self.assertEqual(violation.field, "advisor")

    def test_guard_ignores_same_service_and_unconstrained_relations(self) -> None:
        same_service = models.ForeignKey("academic.Course", on_delete=models.CASCADE, db_constraint=True)
        self.assertIsNone(violation_for("academic", "academic.9999", AddField(model_name="course", name="department", field=same_service)))
        unconstrained = models.ForeignKey("accounts.User", on_delete=models.CASCADE, db_constraint=False)
        self.assertIsNone(violation_for("theses", "theses.9999", AddField(model_name="thesis", name="advisor", field=unconstrained)))

    def test_configured_aliases_are_all_owned_by_an_app(self) -> None:
        self.assertEqual(aliases_without_mapped_app(), set())
