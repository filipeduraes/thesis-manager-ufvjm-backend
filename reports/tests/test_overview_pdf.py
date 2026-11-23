"""
Tests for ``GET /api/v1/reports/overview/pdf``: permissions, filters, file contract,
audit record and the dedicated ``reports_export`` throttle.

The PDF text is extracted with ``pypdf`` to check that it shows the same backend
figures as the JSON overview.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone as dt_timezone
from io import BytesIO
from typing import Any
from unittest.mock import patch

from django.test import TestCase, override_settings
from pypdf import PdfReader

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditAction, AuditLog
from accounts.services import create_external_invitation
from reports.tests.report_fixtures import TEST_CACHES, ReportScenarioMixin

PDF_URL = "/api/v1/reports/overview/pdf"
OVERVIEW_URL = "/api/v1/reports/overview"
EXPORT_RATE_LIMIT = 10
# 17:05 UTC = 14:05 em America/Sao_Paulo (UTC-3, sem horário de verão).
FIXED_NOW = datetime(2026, 11, 19, 17, 5, tzinfo=dt_timezone.utc)


def pdf_text(response: Any) -> str:
    reader = PdfReader(BytesIO(response.content))
    page_texts = [page.extract_text() for page in reader.pages]
    return "\n".join(page_texts)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ReportsOverviewPdfTests(ReportScenarioMixin, TestCase):
    """PDF export of the overview report."""

    def export(self, user: Any, **params: str) -> Any:
        self.client.force_authenticate(user=user)
        return self.client.get(PDF_URL, params)

    def export_text(self, user: Any, **params: str) -> str:
        response = self.export(user, **params)
        self.assertEqual(response.status_code, 200, getattr(response, "data", None))
        return pdf_text(response)

    def export_audit_logs(self) -> Any:
        return AuditLog.objects.filter(action=AuditAction.DOWNLOAD, entity_type="ReportOverview")

    # --- permissões ---

    def test_advisor_student_external_and_anonymous_are_denied(self) -> None:
        self.assertEqual(self.export(self.advisor_1).status_code, 403)
        self.assertEqual(self.export(self.student).status_code, 403)
        invitation = create_external_invitation(name="Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        self.assertEqual(self.export(ExternalInvitationPrincipal(invitation)).status_code, 403)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.get(PDF_URL).status_code, (401, 403))
        self.assertFalse(self.export_audit_logs().exists())

    def test_denied_response_is_json_even_when_only_pdf_is_accepted(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.get(PDF_URL, HTTP_ACCEPT="application/pdf")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("detail", response.json())

    # --- contrato do arquivo ---

    def test_admin_downloads_pdf_with_dated_file_name(self) -> None:
        with patch("reports.views.timezone.now", return_value=FIXED_NOW):
            response = self.export(self.admin)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["Content-Disposition"], 'attachment; filename="relatorio-tcc-20261119-1405.pdf"')
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertTrue(response.content.startswith(b"%PDF-"))
        self.assertIn("19/11/2026 às 14:05 (horário de Brasília)", pdf_text(response))

    def test_pdf_accepts_a_pdf_only_accept_header(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(PDF_URL, HTTP_ACCEPT="application/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_pdf_shows_backend_indicators_and_tables_in_portuguese(self) -> None:
        text = self.export_text(self.admin)
        self.assertIn("Relatório de monografias e bancas", text)
        self.assertIn("Escopo: Todos os cursos", text)
        self.assertIn("Curso: Todos", text)
        self.assertIn("Taxa de aprovação em bancas", text)
        # Mesmos valores de test_admin_summary_indicators (0,6667 e 33,7 dias).
        self.assertIn("66,7%", text)
        self.assertIn("33,7 dias", text)
        self.assertIn("Indicadores por curso", text)
        self.assertIn("Ciência da Computação", text)
        self.assertIn("Sistemas de Informação", text)
        self.assertIn("Indicadores por orientador", text)
        self.assertIn("Ana Orientadora", text)
        self.assertIn("Bruno Orientador", text)
        self.assertIn("Bancas concluídas por ano de defesa", text)
        self.assertIn("2025", text)
        self.assertIn("2026", text)

    # --- filtros ---

    def test_filters_are_applied_and_described_by_name(self) -> None:
        by_year = self.export_text(self.admin, year="2025")
        self.assertIn("Ano de defesa: 2025", by_year)
        self.assertIn("50,0%", by_year)
        self.assertNotIn("Sistemas de Informação", by_year)
        by_course = self.export_text(self.admin, courseId=str(self.course_b.pk))
        self.assertIn("Curso: SI · Sistemas de Informação", by_course)
        self.assertNotIn("Ciência da Computação", by_course)
        by_advisor = self.export_text(self.admin, advisorId=str(self.advisor_2.pk))
        self.assertIn("Orientador: Bruno Orientador", by_advisor)
        self.assertNotIn("Ana Orientadora", by_advisor)

    def test_invalid_filters_return_json_400_without_audit(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(PDF_URL, {"year": "ano"}, HTTP_ACCEPT="application/pdf")
        self.assertEqual(response.status_code, 400)
        self.assertIn("year", response.json())
        self.assertEqual(self.export(self.admin, courseId="abc").status_code, 400)
        self.assertEqual(self.export(self.admin, scope="everything").status_code, 400)
        self.assertFalse(self.export_audit_logs().exists())

    def test_coordinator_pdf_is_limited_to_coordinated_courses(self) -> None:
        text = self.export_text(self.coordinator_a)
        self.assertIn("Escopo: Cursos coordenados pelo solicitante", text)
        self.assertIn("Ciência da Computação", text)
        self.assertNotIn("Sistemas de Informação", text)
        self.assertNotIn("Engenharia", text)

    def test_coordinator_course_outside_scope_is_not_named(self) -> None:
        text = self.export_text(self.coordinator_a, courseId=str(self.course_b.pk))
        self.assertIn("Curso: Curso fora do escopo do solicitante", text)
        self.assertNotIn("Sistemas de Informação", text)
        self.assertIn("Nenhum dado para os filtros selecionados.", text)

    def test_admin_scope_coordinated_restricts_to_own_courses(self) -> None:
        text = self.export_text(self.admin, scope="coordinated")
        self.assertIn("Escopo: Cursos coordenados pelo solicitante", text)
        self.assertNotIn("Ciência da Computação", text)

    # --- auditoria ---

    def test_export_is_audited_as_download_with_identifiers_only(self) -> None:
        self.export_text(self.admin, courseId=str(self.course_a.pk), year="2025", scope="coordinated")
        logs = list(self.export_audit_logs())
        self.assertEqual(len(logs), 1)
        audit_entry = logs[0]
        self.assertEqual(audit_entry.actor_user, self.admin)
        self.assertEqual(audit_entry.entity_id, "overview")
        self.assertEqual(audit_entry.entity_app_label, "reports")
        expected_metadata = {"format": "pdf", "scope": "COORDINATED_COURSES", "courseId": str(self.course_a.pk), "advisorId": None, "year": 2025}
        self.assertEqual(audit_entry.metadata, expected_metadata)
        serialized = json.dumps({"metadata": audit_entry.metadata, "changes": audit_entry.changes})
        for personal_value in ("Ciência da Computação", "Admin", "admin@ufvjm.edu.br"):
            self.assertNotIn(personal_value, serialized)

    # --- limite de requisições ---

    def test_export_has_its_own_throttle_scope(self) -> None:
        for _attempt in range(EXPORT_RATE_LIMIT):
            self.assertEqual(self.export(self.coordinator_a).status_code, 200)
        throttled = self.export(self.coordinator_a)
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(throttled["Content-Type"], "application/json")
        self.assertEqual(self.export_audit_logs().count(), EXPORT_RATE_LIMIT)
        self.assertEqual(self.client.get(OVERVIEW_URL).status_code, 200)
        self.assertEqual(self.export(self.coordinator_b).status_code, 200)
