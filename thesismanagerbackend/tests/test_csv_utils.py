"""Regression tests for spreadsheet formula neutralisation in CSV exports."""

from __future__ import annotations

from django.test import SimpleTestCase

from reports.csv_export import render_overview_csv
from thesismanagerbackend.csv_utils import escape_formula_cell


class CsvFormulaSafetyTests(SimpleTestCase):
    """User-controlled cell values must not become spreadsheet formulas."""

    def test_escape_formula_cell(self) -> None:
        self.assertEqual(escape_formula_cell("=HYPERLINK(\"http://example.com\")"), "'=HYPERLINK(\"http://example.com\")")
        self.assertEqual(escape_formula_cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(escape_formula_cell("Nome normal"), "Nome normal")
        self.assertEqual(escape_formula_cell(42), 42)
        self.assertIsNone(escape_formula_cell(None))

    def test_report_csv_escapes_names(self) -> None:
        payload = {
            "summary": {},
            "byCourse": [{"name": "=1+1", "thesisCount": 1}],
            "byAdvisor": [{"advisorName": "+cmd", "thesisCount": 1}],
            "byDefenseYear": [],
        }
        content = render_overview_csv(payload).decode("utf-8-sig")
        self.assertIn("'=1+1", content)
        self.assertIn("'+cmd", content)
