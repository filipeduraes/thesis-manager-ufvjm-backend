"""
Aggregations computed exclusively from the reporting read model.

All queries use database `Count`/`Avg` so the number of queries does not grow
with the amount of data.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Avg, Count, F, Q

from reports_readmodel.models import ReportFact
from theses.models import ThesisStatus

APPROVED_RESULTS = ("APPROVED", "APPROVED_WITH_REMARKS")
CLOSED_STATUSES = (ThesisStatus.APPROVED, ThesisStatus.APPROVED_WITH_REMARKS, ThesisStatus.REJECTED, ThesisStatus.PUBLISHED)


def _base(filters: dict[str, Any]):
    """Return facts filtered by the requested course, advisor and year."""
    queryset = ReportFact.objects.all()
    if filters.get("course_id"):
        queryset = queryset.filter(course_id=filters["course_id"])
    if filters.get("advisor_id"):
        queryset = queryset.filter(advisor_id=filters["advisor_id"])
    if filters.get("year"):
        queryset = queryset.filter(defense_year=filters["year"])
    return queryset


def _duration_expression():
    """Duration between submission and defense, computed in the database."""
    return F("defense_at") - F("submitted_at")


def _days(value) -> float | None:
    """Convert an aggregate duration to days with one decimal."""
    return round(value.total_seconds() / 86400, 1) if value is not None else None


def _summary(queryset) -> dict[str, Any]:
    """Compute headline indicators with a single aggregate query."""
    row = queryset.aggregate(
        total=Count("thesis_id"),
        published=Count("thesis_id", filter=Q(is_published=True)),
        in_progress=Count("thesis_id", filter=Q(thesis_status=ThesisStatus.UNDER_EVALUATION)),
        defenses_completed=Count("thesis_id", filter=Q(result__gt="")),
        approved=Count("thesis_id", filter=Q(result="APPROVED")),
        approved_with_remarks=Count("thesis_id", filter=Q(result="APPROVED_WITH_REMARKS")),
        rejected=Count("thesis_id", filter=Q(result="REJECTED")),
        avg_days=Avg(_duration_expression(), filter=Q(submitted_at__isnull=False, defense_at__isnull=False)),
    )
    completed = row["defenses_completed"]
    favorable = row["approved"] + row["approved_with_remarks"]
    rate = round(favorable * 100 / completed, 1) if completed else None
    return {
        "thesisCount": row["total"],
        "inProgressCount": row["in_progress"],
        "publishedCount": row["published"],
        "defensesCompletedCount": completed,
        "approvedCount": row["approved"],
        "approvedWithRemarksCount": row["approved_with_remarks"],
        "rejectedCount": row["rejected"],
        "approvalRate": rate,
        "averageDaysToDefense": _days(row["avg_days"]),
    }


def _group(queryset, field: str, label_field: str) -> list[dict[str, Any]]:
    """Group facts by one dimension."""
    rows = queryset.values(field, label_field).annotate(
        total=Count("thesis_id"),
        completed=Count("thesis_id", filter=Q(result__gt="")),
        approved=Count("thesis_id", filter=Q(result="APPROVED")),
        with_remarks=Count("thesis_id", filter=Q(result="APPROVED_WITH_REMARKS")),
        rejected=Count("thesis_id", filter=Q(result="REJECTED")),
        avg_days=Avg(_duration_expression(), filter=Q(submitted_at__isnull=False, defense_at__isnull=False)),
    ).order_by(label_field)
    result = []
    for row in rows:
        favorable = row["approved"] + row["with_remarks"]
        result.append({
            "id": str(row[field]) if row[field] else None,
            "name": row[label_field] or "—",
            "thesisCount": row["total"],
            "defensesCompletedCount": row["completed"],
            "approvalRate": round(favorable * 100 / row["completed"], 1) if row["completed"] else None,
            "averageDaysToDefense": _days(row["avg_days"]),
        })
    return result


def get_overview_payload(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the overview payload from the reporting database."""
    filters = filters or {}
    queryset = _base(filters)
    courses = ReportFact.objects.exclude(course_id__isnull=True).values("course_id", "course_name").distinct().order_by("course_name")
    advisors = ReportFact.objects.exclude(advisor_id__isnull=True).values("advisor_id", "advisor_name").distinct().order_by("advisor_name")
    years = ReportFact.objects.exclude(defense_year__isnull=True).values_list("defense_year", flat=True).distinct().order_by("-defense_year")
    return {
        "scope": "ALL",
        "filters": {
            "courseId": str(filters["course_id"]) if filters.get("course_id") else None,
            "advisorId": str(filters["advisor_id"]) if filters.get("advisor_id") else None,
            "year": filters.get("year"),
        },
        "summary": _summary(queryset),
        "byCourse": _group(queryset, "course_id", "course_name"),
        "byAdvisor": _group(queryset, "advisor_id", "advisor_name"),
        "byDefenseYear": [
            {"year": year, "defensesCompletedCount": queryset.filter(defense_year=year, result__gt="").count()}
            for year in years
        ],
        "options": {
            "courses": [{"id": str(c["course_id"]), "name": c["course_name"]} for c in courses],
            "advisors": [{"id": str(a["advisor_id"]), "name": a["advisor_name"]} for a in advisors],
            "years": list(years),
        },
    }
