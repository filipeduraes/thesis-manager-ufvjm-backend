"""Tests of the overview CSV export and report execution history."""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import AuditAction, AuditLog, ProfileRole, User, UserProfileRole
from reports.models import ReportExecution
from reports.tests.report_fixtures import ReportScenarioMixin


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class OverviewCsvTests(ReportScenarioMixin, TestCase):
    """CSV export formats the cached payload and records the execution."""

    def setUp(self) -> None:
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_csv_download_is_utf8_and_audited(self) -> None:
        response = self.client.get("/api/v1/reports/overview/csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn("Taxa de aprovação em bancas (%)", response.content.decode("utf-8-sig"))
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DOWNLOAD, metadata__format="csv").exists())

    def test_executions_are_recorded_for_json_pdf_and_csv(self) -> None:
        ReportExecution.objects.all().delete()
        self.client.get("/api/v1/reports/overview")
        self.client.get("/api/v1/reports/overview/pdf")
        self.client.get("/api/v1/reports/overview/csv")
        formats = set(ReportExecution.objects.values_list("format", flat=True))
        self.assertEqual(formats, {"json", "pdf", "csv"})
