"""
PDF rendering of the overview report.

The renderer only formats the payload produced by ``reports.services`` (the same
one returned by ``GET /api/v1/reports/overview``); it never queries the database
nor recomputes indicators.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from reports.services import REPORT_TIME_ZONE, SCOPE_ALL

REPORT_TITLE = "Relatório de monografias e bancas"
APPROVAL_RATE_LABEL = "Taxa de aprovação em bancas"
AVERAGE_TIME_LABEL = "Tempo médio entre submissão e defesa"
NO_VALUE = "—"
# Troca os separadores do formato do Python (1,234.5) pelos do pt-BR (1.234,5).
PT_BR_SEPARATORS = str.maketrans({",": ".", ".": ","})
PAGE_MARGIN = 15 * mm
HEADER_BACKGROUND = colors.HexColor("#1F3A5F")
ZEBRA_BACKGROUND = colors.HexColor("#F2F5F8")
GRID_COLOR = colors.HexColor("#C9D2DC")
NAME_COLUMN_WIDTH = 46 * mm

TableRow = list[Paragraph]


def _styles() -> dict[str, ParagraphStyle]:
    """
    Build the paragraph styles used by the report.

    Returns:
        dict[str, ParagraphStyle]: Styles keyed by usage.
    """
    sample = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("ReportTitle", parent=sample["Title"], fontSize=16, spaceAfter=4),
        "heading": ParagraphStyle("ReportHeading", parent=sample["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4),
        "body": ParagraphStyle("ReportBody", parent=sample["BodyText"], fontSize=9, leading=12),
        "cell": ParagraphStyle("ReportCell", parent=sample["BodyText"], fontSize=8, leading=10),
        "number": ParagraphStyle("ReportNumber", parent=sample["BodyText"], fontSize=8, leading=10, alignment=2),
        "header": ParagraphStyle("ReportHeader", parent=sample["BodyText"], fontSize=8, leading=10, textColor=colors.white, fontName="Helvetica-Bold"),
    }


def format_count(value: int) -> str:
    """
    Format an integer counter with pt-BR thousands grouping.

    Args:
        value: Counter from the payload.

    Returns:
        str: Value such as ``1.234``.
    """
    grouped = f"{value:,}"
    return grouped.translate(PT_BR_SEPARATORS)


def format_rate(rate: float | None) -> str:
    """
    Format a 0–1 ratio as a pt-BR percentage with one decimal place.

    Args:
        rate: Ratio from the payload, or None without defenses with a result.

    Returns:
        str: Value such as ``66,7%``, or an em dash.
    """
    if rate is None:
        return NO_VALUE
    percentage = f"{rate * 100:.1f}"
    return f"{percentage.translate(PT_BR_SEPARATORS)}%"


def format_days(days: float | None) -> str:
    """
    Format an average number of days the same way as the frontend.

    Args:
        days: Average from the payload (one decimal place), or None without data.

    Returns:
        str: Value such as ``33,7 dias`` or ``1 dia``, or an em dash.
    """
    if days is None:
        return NO_VALUE
    grouped = f"{days:,.1f}"
    number = grouped.translate(PT_BR_SEPARATORS)
    number = number.removesuffix(",0")
    unit = "dia" if days == 1 else "dias"
    return f"{number} {unit}"


def format_generated_at(generated_at: datetime) -> str:
    """
    Format the generation timestamp in America/Sao_Paulo.

    Args:
        generated_at: Timezone-aware generation time.

    Returns:
        str: Value such as ``19/11/2026 às 14:05 (horário de Brasília)``.
    """
    local_time = generated_at.astimezone(REPORT_TIME_ZONE)
    return f"{local_time:%d/%m/%Y} às {local_time:%H:%M} (horário de Brasília)"


def report_pdf_filename(generated_at: datetime) -> str:
    """
    Build the download file name from the generation time (America/Sao_Paulo).

    Args:
        generated_at: Timezone-aware generation time.

    Returns:
        str: Name such as ``relatorio-tcc-20261119-1405.pdf``.
    """
    local_time = generated_at.astimezone(REPORT_TIME_ZONE)
    return f"relatorio-tcc-{local_time:%Y%m%d-%H%M}.pdf"


def describe_filters(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Describe scope and applied filters in readable Portuguese.

    Names come from the payload ``options``; a filter outside the caller's
    options (for example a course the coordinator does not coordinate) is
    described as unavailable instead of exposing its identifier.

    Args:
        payload: Overview payload.

    Returns:
        list[tuple[str, str]]: (label, value) pairs.
    """
    applied_filters: dict[str, Any] = payload["filters"]
    options: dict[str, list[Any]] = payload["options"]
    scope_text = "Todos os cursos" if payload["scope"] == SCOPE_ALL else "Cursos coordenados pelo solicitante"
    course_text = "Todos"
    course_id = applied_filters.get("courseId")
    if course_id:
        course_names = {course["id"]: f"{course['code']} · {course['name']}" for course in options["courses"]}
        course_text = course_names.get(course_id, "Curso fora do escopo do solicitante")
    advisor_text = "Todos"
    advisor_id = applied_filters.get("advisorId")
    if advisor_id:
        advisor_names = {advisor["id"]: advisor["name"] for advisor in options["advisors"]}
        advisor_text = advisor_names.get(advisor_id, "Orientador sem monografias no escopo")
    year = applied_filters.get("year")
    year_text = str(year) if year else "Todos"
    return [("Escopo", scope_text), ("Curso", course_text), ("Orientador", advisor_text), ("Ano de defesa", year_text)]


def _summary_rows(summary: dict[str, Any]) -> list[tuple[str, str]]:
    """
    List the summary indicators as (label, formatted value) pairs.

    Args:
        summary: ``summary`` section of the payload.

    Returns:
        list[tuple[str, str]]: Indicators in display order.
    """
    return [
        ("Total de monografias", format_count(summary["thesisCount"])),
        ("Em andamento", format_count(summary["inProgressCount"])),
        ("Publicadas", format_count(summary["publishedCount"])),
        ("Bancas agendadas", format_count(summary["scheduledDefenseCount"])),
        ("Bancas concluídas", format_count(summary["completedDefenseCount"])),
        ("Aprovadas", format_count(summary["approvedCount"])),
        ("Aprovadas com ressalvas", format_count(summary["approvedWithRemarksCount"])),
        ("Reprovadas", format_count(summary["rejectedCount"])),
        (APPROVAL_RATE_LABEL, format_rate(summary["approvalRate"])),
        (AVERAGE_TIME_LABEL, format_days(summary["averageSubmissionToDefenseDays"])),
    ]


def _group_values(row: dict[str, Any]) -> list[str]:
    """
    Format the thesis and defense counters shared by course and advisor rows.

    Args:
        row: One ``byCourse`` or ``byAdvisor`` row.

    Returns:
        list[str]: Formatted counters in column order.
    """
    return [
        format_count(row["thesisCount"]),
        format_count(row["inProgressCount"]),
        format_count(row["publishedCount"]),
        format_count(row["completedDefenseCount"]),
        format_rate(row["approvalRate"]),
        format_days(row["averageSubmissionToDefenseDays"]),
    ]


def _year_values(row: dict[str, Any]) -> list[str]:
    """
    Format the defense counters of one ``byDefenseYear`` row.

    Args:
        row: One ``byDefenseYear`` row.

    Returns:
        list[str]: Formatted counters in column order.
    """
    return [
        format_count(row["completedDefenseCount"]),
        format_count(row["approvedCount"]),
        format_count(row["approvedWithRemarksCount"]),
        format_count(row["rejectedCount"]),
        format_rate(row["approvalRate"]),
        format_days(row["averageSubmissionToDefenseDays"]),
    ]


GROUP_HEADERS = ["Monografias", "Em andamento", "Publicadas", "Bancas concluídas", APPROVAL_RATE_LABEL, "Tempo médio"]
YEAR_HEADERS = ["Bancas concluídas", "Aprovadas", "Com ressalvas", "Reprovadas", APPROVAL_RATE_LABEL, "Tempo médio"]


def _data_table(headers: list[str], rows: list[list[str]], styles: dict[str, ParagraphStyle], first_column_width: float) -> Table:
    """
    Build a striped table whose header repeats on every page.

    Cells are escaped paragraphs so names with ``&`` or ``<`` render literally
    and long names wrap instead of overflowing the page.

    Args:
        headers: Column titles.
        rows: Formatted cell values; the first column is text, the rest numbers.
        styles: Paragraph styles from ``_styles``.
        first_column_width: Width of the name column; the rest share the remainder.

    Returns:
        Table: Flowable table.
    """
    header_row: TableRow = [Paragraph(escape(title), styles["header"]) for title in headers]
    body_rows: list[TableRow] = []
    for values in rows:
        name_cell = Paragraph(escape(values[0]), styles["cell"])
        number_cells = [Paragraph(escape(value), styles["number"]) for value in values[1:]]
        body_rows.append([name_cell, *number_cells])
    available_width = A4[0] - 2 * PAGE_MARGIN
    number_width = (available_width - first_column_width) / (len(headers) - 1)
    column_widths = [first_column_width] + [number_width] * (len(headers) - 1)
    table = Table([header_row, *body_rows], colWidths=column_widths, repeatRows=1)
    table_style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BACKGROUND),
        ("GRID", (0, 0), (-1, -1), 0.4, GRID_COLOR),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
    for row_index in range(2, len(body_rows) + 1, 2):
        table_style.append(("BACKGROUND", (0, row_index), (-1, row_index), ZEBRA_BACKGROUND))
    table.setStyle(TableStyle(table_style))
    return table


def _section(title: str, table: Table | None, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    """
    Build a titled section, or an empty-state sentence when there are no rows.

    Args:
        title: Section heading.
        table: Section table, or None when there is no data.
        styles: Paragraph styles from ``_styles``.

    Returns:
        list[Flowable]: Heading and content.
    """
    heading = Paragraph(escape(title), styles["heading"])
    if table is None:
        return [heading, Paragraph("Nenhum dado para os filtros selecionados.", styles["body"])]
    return [heading, table]


def _page_footer(generated_text: str) -> Callable[[Canvas, SimpleDocTemplate], None]:
    """
    Build the callback that draws the footer (generation time and page number).

    Args:
        generated_text: Formatted generation timestamp.

    Returns:
        Callable[[Canvas, SimpleDocTemplate], None]: ReportLab page callback.
    """

    def draw_footer(canvas: Canvas, document: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(PAGE_MARGIN, 8 * mm, f"Thesis Manager UFVJM · gerado em {generated_text}")
        canvas.drawRightString(A4[0] - PAGE_MARGIN, 8 * mm, f"Página {document.page}")
        canvas.restoreState()

    return draw_footer


def render_overview_pdf(payload: dict[str, Any], generated_at: datetime) -> bytes:
    """
    Render the overview payload as an A4 PDF in Portuguese.

    Contents: title, generation time (America/Sao_Paulo), scope and filters,
    summary indicators and the per-course, per-advisor and per-year tables.

    Args:
        payload: Payload from ``get_overview_payload``.
        generated_at: Timezone-aware generation time.

    Returns:
        bytes: PDF document.
    """
    styles = _styles()
    generated_text = format_generated_at(generated_at)
    story: list[Flowable] = [
        Paragraph(REPORT_TITLE, styles["title"]),
        Paragraph(f"Gerado em {escape(generated_text)}", styles["body"]),
        Spacer(1, 4 * mm),
        Paragraph("Filtros aplicados", styles["heading"]),
    ]
    for label, value in describe_filters(payload):
        story.append(Paragraph(f"<b>{escape(label)}:</b> {escape(value)}", styles["body"]))

    story.append(Paragraph("Resumo", styles["heading"]))
    summary_rows = [[Paragraph(escape(label), styles["cell"]), Paragraph(escape(value), styles["number"])] for label, value in _summary_rows(payload["summary"])]
    summary_table = Table(summary_rows, colWidths=[90 * mm, 40 * mm], hAlign="LEFT")
    summary_table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, GRID_COLOR), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story.append(summary_table)

    course_rows = [[f"{row['courseCode']} · {row['courseName']}", *_group_values(row)] for row in payload["byCourse"]]
    course_table = _data_table(["Curso", *GROUP_HEADERS], course_rows, styles, NAME_COLUMN_WIDTH) if course_rows else None
    story.extend(_section("Indicadores por curso", course_table, styles))

    advisor_rows = [[row["advisorName"], *_group_values(row)] for row in payload["byAdvisor"]]
    advisor_table = _data_table(["Orientador", *GROUP_HEADERS], advisor_rows, styles, NAME_COLUMN_WIDTH) if advisor_rows else None
    story.extend(_section("Indicadores por orientador", advisor_table, styles))

    year_rows = [[str(row["year"]), *_year_values(row)] for row in payload["byDefenseYear"]]
    year_table = _data_table(["Ano de defesa", *YEAR_HEADERS], year_rows, styles, 30 * mm) if year_rows else None
    story.extend(_section("Bancas concluídas por ano de defesa", year_table, styles))

    buffer = BytesIO()
    document = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=PAGE_MARGIN, rightMargin=PAGE_MARGIN, topMargin=PAGE_MARGIN, bottomMargin=PAGE_MARGIN + 5 * mm, title=REPORT_TITLE, author="Thesis Manager UFVJM", lang="pt-BR")
    draw_footer = _page_footer(generated_text)
    document.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
