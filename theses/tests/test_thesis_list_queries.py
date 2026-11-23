"""
Regression test: the authenticated thesis list must not grow queries with rows.
"""

from __future__ import annotations

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from theses.models import ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class ThesisListQueryCountTests(RepositoryTestCase):
    """Viewer roles are resolved once per page, not once per thesis."""

    def test_query_count_is_constant_for_two_and_six_theses(self) -> None:
        cache.clear()
        self.client.force_authenticate(user=self.student)
        for index in range(2):
            create_thesis(ThesisSpec(title=f"Tese {index}", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["consulta"], status=ThesisStatus.SUBMITTED))
        with CaptureQueriesContext(connection) as small:
            response = self.client.get("/api/v1/theses")
        self.assertEqual(response.status_code, 200)
        for index in range(2, 6):
            create_thesis(ThesisSpec(title=f"Tese {index}", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["consulta"], status=ThesisStatus.SUBMITTED))
        cache.clear()
        with CaptureQueriesContext(connection) as large:
            response = self.client.get("/api/v1/theses")
        self.assertEqual(response.status_code, 200)
        print("SQL_LARGE", [q["sql"][:90] for q in large.captured_queries])
        self.assertLessEqual(len(large) - len(small), 2, f"2 theses: {len(small)} queries; 6 theses: {len(large)}")
