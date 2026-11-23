"""
Database-side aggregates for the reporting dashboards.

Every indicator is computed by the ORM (``Count``/``Avg`` with ``GROUP BY``);
Python only merges already-aggregated rows and formats them as camelCase payloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.db.models import Avg, CharField, Count, DurationField, ExpressionWrapper, F, OuterRef, Q, QuerySet, Subquery
from django.db.models.fields.json import KT
from django.db.models.functions import Cast, ExtractYear

from academic.models import Course
from accounts.models import User
from theses.models import Defense, DefenseResult, DefenseStatus, Thesis, ThesisStatus, ThesisStatusHistory
from theses.services import coordinated_courses_filter, user_is_admin, user_is_coordinator

REPORT_CACHE_TTL_SECONDS = 60
REPORT_TIME_ZONE = ZoneInfo("America/Sao_Paulo")
SECONDS_PER_DAY = 86400

# Chaves gravadas em ThesisStatusHistory.metadata por
# theses.defense_scheduling._finalize_defense. O modelo Defense não guarda o
# resultado; se essas chaves mudarem de nome lá, os testes de reports falham.
DEFENSE_ID_METADATA_KEY = "defenseId"
DEFENSE_RESULT_METADATA_KEY = "defenseResult"

SCOPE_ALL = "ALL"
SCOPE_COORDINATED_COURSES = "COORDINATED_COURSES"

IN_PROGRESS_THESIS_STATUSES: tuple[str, ...] = (
    ThesisStatus.SUBMITTED,
    ThesisStatus.ADVISOR_REVIEW,
    ThesisStatus.CHANGES_REQUESTED,
    ThesisStatus.ADVISOR_APPROVED,
    ThesisStatus.READY_FOR_DEFENSE,
    ThesisStatus.DEFENSE_SCHEDULED,
    ThesisStatus.UNDER_EVALUATION,
    ThesisStatus.CORRECTIONS_PENDING,
)


@dataclass(frozen=True)
class ReportFilters:
    """
    Validated filters of the overview report.

    Attributes:
        course_id: Restrict to one course (intersected with the caller's scope).
        advisor_id: Restrict to theses advised by this user.
        year: Defense year (America/Sao_Paulo) of completed or scheduled defenses.
        coordinated_only: Restrict an administrator to the courses they coordinate.
    """

    course_id: UUID | None = None
    advisor_id: UUID | None = None
    year: int | None = None
    coordinated_only: bool = False


@dataclass(frozen=True)
class ReportScope:
    """
    Course scope resolved for the caller.

    Attributes:
        code: ``ALL`` for administrators, ``COORDINATED_COURSES`` otherwise.
        course_ids: IDs of courses in scope, or ``None`` when every course is visible.
        cache_owner: Cache namespace shared by callers who see the same data.
    """

    code: str
    course_ids: frozenset[UUID] | None
    cache_owner: str


class DefenseCounters(TypedDict):
    """
    Defense-level counters shared by summary and group rows.
    """

    completedDefenseCount: int
    approvedCount: int
    approvedWithRemarksCount: int
    rejectedCount: int
    approvalRate: float | None
    averageSubmissionToDefenseDays: float | None


def resolve_report_scope(user: User, filters: ReportFilters) -> ReportScope:
    """
    Decide which courses the caller may aggregate.

    Administrators see every course unless they ask for ``coordinated_only``;
    everyone else (coordinators, and staff without the ADMIN role) is limited to
    the courses linked through ``coordinated_courses_filter``. A caller without
    the coordinator role in that branch gets an empty scope.

    Args:
        user: Authenticated institutional user already allowed by the view.
        filters: Validated filters (only ``coordinated_only`` matters here).

    Returns:
        ReportScope: Scope code, course ids and cache namespace.
    """
    if user_is_admin(user) and not filters.coordinated_only:
        return ReportScope(code=SCOPE_ALL, course_ids=None, cache_owner="all")
    if not user_is_coordinator(user):
        return ReportScope(code=SCOPE_COORDINATED_COURSES, course_ids=frozenset(), cache_owner=f"user:{user.pk}")
    course_filter = coordinated_courses_filter(user)
    # O banco de cursos é outro; materializar os IDs evita subquery cross-DB.
    coordinated_ids = frozenset(Course.objects.filter(course_filter).values_list("pk", flat=True))
    return ReportScope(code=SCOPE_COORDINATED_COURSES, course_ids=coordinated_ids, cache_owner=f"user:{user.pk}")


def overview_cache_key(scope: ReportScope, filters: ReportFilters) -> str:
    """
    Build the namespaced cache key of an overview response.

    Args:
        scope: Caller scope (administrators share one namespace).
        filters: Validated filters.

    Returns:
        str: Cache key without personal data beyond the user id.
    """
    return f"reports:overview:{scope.cache_owner}:{filters.course_id or '-'}:{filters.advisor_id or '-'}:{filters.year or '-'}"


def _scoped_theses(scope: ReportScope, filters: ReportFilters) -> QuerySet[Thesis]:
    """
    Return non-draft, non-deleted theses within scope, course and advisor filters.

    Args:
        scope: Caller scope.
        filters: Validated filters (``year`` is applied by the callers).

    Returns:
        QuerySet[Thesis]: Theses counted by the report.
    """
    queryset = Thesis.objects.filter(deleted_at__isnull=True).exclude(status=ThesisStatus.DRAFT)
    if scope.course_ids is not None:
        queryset = queryset.filter(course_id__in=scope.course_ids)
    if filters.course_id:
        queryset = queryset.filter(course_id=filters.course_id)
    if filters.advisor_id:
        queryset = queryset.filter(advisor_id=filters.advisor_id)
    return queryset


def _defense_result_subquery() -> Subquery:
    """
    Read the committee result recorded by ``_finalize_defense`` for each defense.

    Depends on the ``defenseId``/``defenseResult`` metadata keys written on the
    thesis status history when a defense closes (see the module constants).

    Returns:
        Subquery: Result code as text, or NULL when the close entry is missing.
    """
    defense_id_text = Cast(OuterRef("pk"), output_field=CharField())
    close_entries = ThesisStatusHistory.objects.filter(thesis_id=OuterRef("thesis_id"))
    close_entries = close_entries.annotate(close_defense_id=KT(f"metadata__{DEFENSE_ID_METADATA_KEY}"))
    close_entries = close_entries.filter(close_defense_id=defense_id_text).order_by("-changed_at")
    result_values = close_entries.values(result_code=KT(f"metadata__{DEFENSE_RESULT_METADATA_KEY}"))
    return Subquery(result_values[:1], output_field=CharField())


def _submission_before_defense_subquery() -> Subquery:
    """
    Return the last submission that preceded each defense.

    ``Thesis.submitted_at`` is overwritten when a rejected thesis is resubmitted,
    so the status history is used to measure each defense against the submission
    that led to it.

    Returns:
        Subquery: Timestamp of the latest SUBMITTED transition up to the defense.
    """
    submissions = ThesisStatusHistory.objects.filter(thesis_id=OuterRef("thesis_id"), new_status=ThesisStatus.SUBMITTED, changed_at__lte=OuterRef("scheduled_datetime"))
    latest_submission = submissions.order_by("-changed_at").values("changed_at")[:1]
    return Subquery(latest_submission)


def _completed_defenses(theses: QuerySet[Thesis], filters: ReportFilters) -> QuerySet[Defense]:
    """
    Return completed defenses of the given theses annotated for aggregation.

    Args:
        theses: Scoped theses.
        filters: Validated filters (``year`` restricts the defense year).

    Returns:
        QuerySet[Defense]: Defenses with ``result_code``, ``defense_year`` and
        ``submission_to_defense`` annotations.
    """
    queryset = Defense.objects.filter(status=DefenseStatus.COMPLETED, thesis__in=theses)
    queryset = queryset.annotate(
        result_code=_defense_result_subquery(),
        submitted_reference=_submission_before_defense_subquery(),
        defense_year=ExtractYear("scheduled_datetime", tzinfo=REPORT_TIME_ZONE),
    )
    duration = ExpressionWrapper(F("scheduled_datetime") - F("submitted_reference"), output_field=DurationField())
    queryset = queryset.annotate(submission_to_defense=duration)
    if filters.year:
        queryset = queryset.filter(defense_year=filters.year)
    return queryset


def _defense_aggregates() -> dict[str, Any]:
    """
    Return the aggregate expressions shared by every defense grouping.

    Returns:
        dict[str, Any]: Aggregations keyed by internal snake_case names.
    """
    return {
        "completed_count": Count("pk"),
        "approved_count": Count("pk", filter=Q(result_code=DefenseResult.APPROVED)),
        "with_remarks_count": Count("pk", filter=Q(result_code=DefenseResult.APPROVED_WITH_REMARKS)),
        "rejected_count": Count("pk", filter=Q(result_code=DefenseResult.REJECTED)),
        "average_duration": Avg("submission_to_defense"),
    }


def _thesis_aggregates() -> dict[str, Any]:
    """
    Return the aggregate expressions shared by every thesis grouping.

    Returns:
        dict[str, Any]: Aggregations keyed by internal snake_case names.
    """
    return {
        "thesis_count": Count("pk", distinct=True),
        "in_progress_count": Count("pk", filter=Q(status__in=IN_PROGRESS_THESIS_STATUSES), distinct=True),
        "published_count": Count("pk", filter=Q(status=ThesisStatus.PUBLISHED), distinct=True),
    }


def _days(duration: timedelta | None) -> float | None:
    """
    Convert an average duration into days with one decimal place.

    Args:
        duration: Average computed by the database, or None without data.

    Returns:
        float | None: Days, or None when there is no measured defense.
    """
    if duration is None:
        return None
    return round(duration.total_seconds() / SECONDS_PER_DAY, 1)


def _defense_counters(row: dict[str, Any] | None) -> DefenseCounters:
    """
    Format one aggregated defense row; missing rows become zero counters.

    The approval rate is (approved + approved with remarks) / defenses with a
    recorded result, and is None when no defense has a result.

    Args:
        row: Aggregated values from ``_defense_aggregates``, or None.

    Returns:
        DefenseCounters: camelCase counters.
    """
    row = row or {}
    approved = row.get("approved_count") or 0
    with_remarks = row.get("with_remarks_count") or 0
    rejected = row.get("rejected_count") or 0
    decided = approved + with_remarks + rejected
    approval_rate = round((approved + with_remarks) / decided, 4) if decided else None
    return {
        "completedDefenseCount": row.get("completed_count") or 0,
        "approvedCount": approved,
        "approvedWithRemarksCount": with_remarks,
        "rejectedCount": rejected,
        "approvalRate": approval_rate,
        "averageSubmissionToDefenseDays": _days(row.get("average_duration")),
    }


def _thesis_counters(row: dict[str, Any] | None) -> dict[str, int]:
    """
    Format one aggregated thesis row; missing rows become zero counters.

    Args:
        row: Aggregated values from ``_thesis_aggregates``, or None.

    Returns:
        dict[str, int]: camelCase thesis counters.
    """
    row = row or {}
    return {
        "thesisCount": row.get("thesis_count") or 0,
        "inProgressCount": row.get("in_progress_count") or 0,
        "publishedCount": row.get("published_count") or 0,
    }


def _by_course(theses: QuerySet[Thesis], defenses: QuerySet[Defense]) -> list[dict[str, Any]]:
    """
    Group thesis and defense counters by course.

    Args:
        theses: Theses already narrowed by every filter.
        defenses: Completed defenses of those theses.

    Returns:
        list[dict[str, Any]]: One row per course with data, ordered by name.
    """
    thesis_rows = theses.values("course_id", "course_code", "course_name").annotate(**_thesis_aggregates()).order_by("course_name")
    defense_rows = defenses.values("thesis__course_id").annotate(**_defense_aggregates()).order_by()
    defenses_by_course = {row["thesis__course_id"]: row for row in defense_rows}
    rows: list[dict[str, Any]] = []
    for thesis_row in thesis_rows:
        course_id = thesis_row["course_id"]
        rows.append(
            {
                "courseId": str(course_id),
                "courseCode": thesis_row["course_code"],
                "courseName": thesis_row["course_name"],
                **_thesis_counters(thesis_row),
                **_defense_counters(defenses_by_course.get(course_id)),
            }
        )
    return rows


def _by_advisor(theses: QuerySet[Thesis], defenses: QuerySet[Defense]) -> list[dict[str, Any]]:
    """
    Group thesis and defense counters by advisor.

    Args:
        theses: Theses already narrowed by every filter.
        defenses: Completed defenses of those theses.

    Returns:
        list[dict[str, Any]]: One row per advisor, most theses first, then by name.
    """
    thesis_rows = theses.values("advisor_id", "advisor_name").annotate(**_thesis_aggregates()).order_by("-thesis_count", "advisor_name")
    defense_rows = defenses.values("thesis__advisor_id").annotate(**_defense_aggregates()).order_by()
    defenses_by_advisor = {row["thesis__advisor_id"]: row for row in defense_rows}
    rows: list[dict[str, Any]] = []
    for thesis_row in thesis_rows:
        advisor_id = thesis_row["advisor_id"]
        rows.append(
            {
                "advisorId": str(advisor_id),
                "advisorName": thesis_row["advisor_name"],
                **_thesis_counters(thesis_row),
                **_defense_counters(defenses_by_advisor.get(advisor_id)),
            }
        )
    return rows


def _by_defense_year(defenses: QuerySet[Defense]) -> list[dict[str, Any]]:
    """
    Group defense counters by defense year (America/Sao_Paulo).

    Args:
        defenses: Completed defenses already narrowed by every filter.

    Returns:
        list[dict[str, Any]]: One row per year, oldest first.
    """
    year_rows = defenses.values("defense_year").annotate(**_defense_aggregates()).order_by("defense_year")
    return [{"year": row["defense_year"], **_defense_counters(row)} for row in year_rows]


def _options(scope: ReportScope) -> dict[str, list[Any]]:
    """
    List the filter values available to the caller, ignoring the applied filters.

    Args:
        scope: Caller scope.

    Returns:
        dict[str, list[Any]]: ``courses``, ``advisors`` and defense ``years``.
    """
    courses = Course.objects.all()
    if scope.course_ids is not None:
        courses = courses.filter(pk__in=scope.course_ids)
    course_rows = courses.order_by("name").values("id", "code", "name")
    scoped_theses = _scoped_theses(scope, ReportFilters())
    advisor_rows = scoped_theses.order_by("advisor_name").values("advisor_id", "advisor_name").distinct()
    year_rows = _completed_defenses(scoped_theses, ReportFilters()).values_list("defense_year", flat=True).distinct().order_by("defense_year")
    return {
        "courses": [{"id": str(row["id"]), "code": row["code"], "name": row["name"]} for row in course_rows],
        "advisors": [{"id": str(row["advisor_id"]), "name": row["advisor_name"]} for row in advisor_rows],
        "years": list(year_rows),
    }


def build_overview(scope: ReportScope, filters: ReportFilters) -> dict[str, Any]:
    """
    Compute the overview report: summary, per-course, per-advisor and per-year rows.

    Definitions (also documented in AGENTS.md and FUNCIONALIDADES.md):
    theses are non-deleted and non-draft; defense counters use COMPLETED
    defenses and the result recorded by ``_finalize_defense`` in
    ``ThesisStatusHistory.metadata`` (keys ``defenseId``/``defenseResult``);
    with ``year`` the theses are those with a completed defense in that year and
    scheduled defenses are those scheduled for that year.

    Args:
        scope: Caller scope from ``resolve_report_scope``.
        filters: Validated filters.

    Returns:
        dict[str, Any]: camelCase payload of ``GET /api/v1/reports/overview``.
    """
    base_theses = _scoped_theses(scope, filters)
    defenses = _completed_defenses(base_theses, filters)
    scheduled = Defense.objects.filter(status=DefenseStatus.SCHEDULED, thesis__in=base_theses)
    theses = base_theses
    if filters.year:
        theses = base_theses.filter(pk__in=defenses.values("thesis_id"))
        scheduled = scheduled.annotate(defense_year=ExtractYear("scheduled_datetime", tzinfo=REPORT_TIME_ZONE))
        scheduled = scheduled.filter(defense_year=filters.year)
    thesis_summary = theses.aggregate(**_thesis_aggregates())
    defense_summary = defenses.aggregate(**_defense_aggregates())
    return {
        "scope": scope.code,
        "filters": {
            "courseId": str(filters.course_id) if filters.course_id else None,
            "advisorId": str(filters.advisor_id) if filters.advisor_id else None,
            "year": filters.year,
        },
        "summary": {
            **_thesis_counters(thesis_summary),
            "scheduledDefenseCount": scheduled.count(),
            **_defense_counters(defense_summary),
        },
        "byCourse": _by_course(theses, defenses),
        "byAdvisor": _by_advisor(theses, defenses),
        "byDefenseYear": _by_defense_year(defenses),
        "options": _options(scope),
    }


def get_overview_payload(scope: ReportScope, filters: ReportFilters) -> dict[str, Any]:
    """
    Return the overview payload from the cache, computing and caching it on a miss.

    Shared by the JSON overview and the PDF export so both show the same figures
    within the cache window.

    Args:
        scope: Caller scope from ``resolve_report_scope``.
        filters: Validated filters.

    Returns:
        dict[str, Any]: camelCase payload of ``GET /api/v1/reports/overview``.
    """
    cache_key = overview_cache_key(scope, filters)
    cached_payload: dict[str, Any] | None = cache.get(cache_key)
    if cached_payload is not None:
        return cached_payload
    payload = build_overview(scope, filters)
    cache.set(cache_key, payload, REPORT_CACHE_TTL_SECONDS)
    return payload
