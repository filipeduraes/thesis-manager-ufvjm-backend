"""
CSV rendering of the overview report.

The renderer only formats the cached payload; it never queries the database.
"""

from __future__ import annotations

import csv
import io
from typing import Any


def render_overview_csv(payload: dict[str, Any]) -> bytes:
    """
    Build a UTF-8 (BOM) CSV with the overview indicators.

    Args:
        payload: Overview payload produced by ``get_overview_payload``.

    Returns:
        bytes: CSV content suitable for an attachment response.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    summary = payload.get("summary", {})
    writer.writerow(["Relatório de monografias e bancas"])
    writer.writerow([])
    writer.writerow(["Indicador", "Valor"])
    labels = {
        "thesisCount": "Total de monografias",
        "inProgressCount": "Em andamento",
        "publishedCount": "Publicadas",
        "defensesCompletedCount": "Bancas concluídas",
        "approvedCount": "Aprovadas",
        "approvedWithRemarksCount": "Aprovadas com ressalvas",
        "rejectedCount": "Reprovadas",
        "approvalRate": "Taxa de aprovação em bancas (%)",
        "averageDaysToDefense": "Tempo médio entre submissão e defesa (dias)",
    }
    for key, label in labels.items():
        writer.writerow([label, summary.get(key, "")])
    for section, columns in (
        ("byCourse", ("Curso", "Monografias", "Bancas concluídas", "Taxa de aprovação (%)", "Tempo médio (dias)")),
        ("byAdvisor", ("Orientador", "Monografias", "Bancas concluídas", "Taxa de aprovação (%)", "Tempo médio (dias)")),
    ):
        writer.writerow([])
        writer.writerow(list(columns))
        for row in payload.get(section, []):
            name = row.get("name") or row.get("courseName") or row.get("advisorName") or ""
            writer.writerow([
                name,
                row.get("thesisCount", ""),
                row.get("defensesCompletedCount", ""),
                row.get("approvalRate", ""),
                row.get("averageDaysToDefense", ""),
            ])
    writer.writerow([])
    writer.writerow(["Ano de defesa", "Bancas concluídas"])
    for row in payload.get("byDefenseYear", []):
        writer.writerow([row.get("year", ""), row.get("defensesCompletedCount", "")])
    return buffer.getvalue().encode("utf-8-sig")
