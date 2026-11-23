"""Regression tests: no SQL query may join a table owned by another service.

The unit suite normally runs with every alias pointing at one SQLite file, so
cross-database JOINs silently succeed there. These tests inspect the generated
SQL instead: with ``SERVICE_DATABASES_SHARED=False`` the same JOIN would hit a
table that does not exist in the thesis connection.
"""

from __future__ import annotations

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from reports.services import ReportFilters, _scoped_theses, resolve_report_scope
from reports.tests.report_fixtures import ReportScenarioMixin
from repository_readmodel.projections import project_thesis
from reports_readmodel.projections import _fact
from theses.models import ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis

FOREIGN_TABLES = ("academic_course", "users_user", "external_invitation")


def _sql(context: CaptureQueriesContext) -> str:
    return " ".join(query["sql"] for query in context.captured_queries)


class ReportScopeQueryTests(ReportScenarioMixin, TestCase):
    """Coordinator scope must be resolved to IDs before querying theses."""

    def test_scope_queryset_does_not_subquery_the_course_table(self) -> None:
        scope = resolve_report_scope(self.coordinator_a, ReportFilters())
        with CaptureQueriesContext(connection) as captured:
            theses = list(_scoped_theses(scope, ReportFilters()))
        sql = _sql(captured)
        for table in FOREIGN_TABLES:
            with self.subTest(table=table):
                self.assertNotIn(table, sql)
        self.assertEqual({str(thesis.pk) for thesis in theses}, {str(self.thesis_1.pk), str(self.thesis_2.pk)})


class ReadModelProjectionQueryTests(RepositoryTestCase):
    """Projections must use snapshot columns instead of joining other services."""

    def test_repository_projection_does_not_join_course_or_user_tables(self) -> None:
        thesis = create_thesis(
            ThesisSpec(
                title="Projeção sem JOIN",
                course=self.course,
                advisor=self.advisor,
                authors=[self.student],
                keywords=["banco"],
                status=ThesisStatus.PUBLISHED,
            )
        )
        with CaptureQueriesContext(connection) as captured:
            project_thesis(str(thesis.pk))
        sql = _sql(captured)
        for table in FOREIGN_TABLES:
            with self.subTest(table=table):
                self.assertNotIn(table, sql)

    def test_reports_projection_does_not_join_course_or_user_tables(self) -> None:
        thesis = create_thesis(
            ThesisSpec(
                title="Fato sem JOIN",
                course=self.course,
                advisor=self.advisor,
                authors=[self.student],
                keywords=["banco"],
                status=ThesisStatus.SUBMITTED,
            )
        )
        with CaptureQueriesContext(connection) as captured:
            fact = _fact(str(thesis.pk))
        sql = _sql(captured)
        for table in FOREIGN_TABLES:
            with self.subTest(table=table):
                self.assertNotIn(table, sql)
        self.assertEqual(fact.course_id, thesis.course_id)
        self.assertEqual(fact.advisor_id, thesis.advisor_id)
