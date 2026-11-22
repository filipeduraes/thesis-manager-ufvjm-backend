"""
Public repository of published theses: search, filters and anonymous payloads.

Everything here is reachable without authentication, so payloads expose only
bibliographic data (names, never e-mails) and public download URLs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, QuerySet, Value, When
from django.db.models.functions import Coalesce, ExtractYear, Lower

from academic.models import Course
from accounts.models import User
from theses.models import Thesis, ThesisAuthor, ThesisFile, ThesisKeyword, ThesisStatus, ThesisType
from theses.services import (
    REPOSITORY_TIME_ZONE,
    STATUS_DESCRIPTIONS,
    WORKFLOW_STEPS,
    course_brief,
    thesis_file_payload,
    thesis_list_queryset,
    thesis_permissions,
)

ORDERING_RELEVANCE = "relevance"
ORDERING_RECENT = "recent"
ORDERING_TITLE = "title"
ORDERING_CHOICES: tuple[str, ...] = (ORDERING_RELEVANCE, ORDERING_RECENT, ORDERING_TITLE)
PUBLIC_KEYWORDS_LIMIT = 100

# Pesos da relevância: o título pesa mais que palavras-chave, que pesam mais que
# pessoas e curso; o resumo só desempata. Calculado com CASE/EXISTS para funcionar
# igual no PostgreSQL e no SQLite da suíte E2E.
RELEVANCE_WEIGHTS: dict[str, int] = {
    "title": 8,
    "keyword": 5,
    "author": 4,
    "advisor": 3,
    "course": 2,
    "abstract": 1,
}


@dataclass(frozen=True)
class PublicSearchFilters:
    """
    Validated search parameters of the public repository.

    Attributes:
        query: Free text matched against title, abstract, authors, advisor, course and keywords.
        title: Text matched against the title only.
        author: Text matched against author names.
        advisor: Text matched against the advisor name.
        course: Text matched against the course name or code.
        keywords: Normalized keywords that must all be present.
        course_id: Exact course identifier.
        advisor_id: Exact advisor identifier.
        year: Defense year.
        ordering: ``relevance``, ``recent`` or ``title``; empty picks the default.
    """

    query: str = ""
    title: str = ""
    author: str = ""
    advisor: str = ""
    course: str = ""
    keywords: tuple[str, ...] = field(default_factory=tuple)
    course_id: UUID | None = None
    advisor_id: UUID | None = None
    year: int | None = None
    ordering: str = ""

    @property
    def effective_ordering(self) -> str:
        """
        Return the ordering applied to the results.

        Returns:
            str: ``relevance`` when there is free text and no explicit choice, otherwise
                the requested ordering or ``recent``.
        """
        if self.ordering:
            if self.ordering == ORDERING_RELEVANCE and not self.query:
                return ORDERING_RECENT
            return self.ordering
        return ORDERING_RELEVANCE if self.query else ORDERING_RECENT

    def cache_material(self) -> dict[str, Any]:
        """
        Return a canonical, JSON-serializable view used to build cache keys.

        Returns:
            dict[str, Any]: Normalized filter values.
        """
        return {
            "q": self.query.casefold(),
            "title": self.title.casefold(),
            "author": self.author.casefold(),
            "advisor": self.advisor.casefold(),
            "course": self.course.casefold(),
            "keywords": sorted(self.keywords),
            "courseId": str(self.course_id) if self.course_id else "",
            "advisorId": str(self.advisor_id) if self.advisor_id else "",
            "year": self.year,
            "ordering": self.effective_ordering,
        }


def published_theses() -> QuerySet[Thesis]:
    """
    Return the base queryset of theses visible in the public repository.

    Returns:
        QuerySet[Thesis]: Published, public and non-deleted theses (no prefetches).
    """
    return Thesis.objects.filter(
        is_public=True,
        status=ThesisStatus.PUBLISHED,
        published_at__isnull=False,
        deleted_at__isnull=True,
    )


def public_theses_queryset() -> QuerySet[Thesis]:
    """
    Return the public repository queryset with the relations read by the payloads.

    Returns:
        QuerySet[Thesis]: Published theses with list relations prefetched.
    """
    return thesis_list_queryset().filter(pk__in=published_theses().values("pk"))


def defense_year_expression() -> Coalesce:
    """
    Build the expression for the defense year of a published thesis.

    ``defense_year`` is filled on publication; the publication year is only a
    safety net for rows written outside the services.

    Returns:
        Coalesce: Year expression usable in filters, annotations and aggregations.
    """
    publication_year = ExtractYear("published_at", tzinfo=REPOSITORY_TIME_ZONE)
    return Coalesce(F("defense_year"), publication_year, output_field=IntegerField())


def _author_name_match(value: str) -> Exists:
    """
    Build an EXISTS filter matching any author whose name contains ``value``.

    Args:
        value: Search fragment.

    Returns:
        Exists: Correlated subquery over ``thesis_author``.
    """
    author_links = ThesisAuthor.objects.filter(thesis=OuterRef("pk"), author__name__icontains=value)
    return Exists(author_links)


def _keyword_name_match(value: str) -> Exists:
    """
    Build an EXISTS filter matching any keyword that contains ``value``.

    Args:
        value: Search fragment.

    Returns:
        Exists: Correlated subquery over ``thesis_keyword``.
    """
    keyword_links = ThesisKeyword.objects.filter(thesis=OuterRef("pk"), keyword__name__icontains=value)
    return Exists(keyword_links)


def _course_match(value: str) -> Q:
    """
    Build the filter matching the course name or code.

    Args:
        value: Search fragment.

    Returns:
        Q: Course filter.
    """
    return Q(course__name__icontains=value) | Q(course__code__iexact=value)


def _free_text_filter(query: str) -> Q:
    """
    Build the free-text filter across every searchable field.

    Args:
        query: Search text.

    Returns:
        Q: Combined filter.
    """
    return (
        Q(title__icontains=query)
        | Q(abstract__icontains=query)
        | Q(advisor__name__icontains=query)
        | _course_match(query)
        | Q(_author_name_match(query))
        | Q(_keyword_name_match(query))
    )


def _relevance_expression(query: str) -> Any:
    """
    Build the relevance score for a free-text query.

    Args:
        query: Search text.

    Returns:
        Any: Integer expression summing the weights of the matched fields.
    """
    conditions = [
        (Q(title__icontains=query), RELEVANCE_WEIGHTS["title"]),
        (Q(_keyword_name_match(query)), RELEVANCE_WEIGHTS["keyword"]),
        (Q(_author_name_match(query)), RELEVANCE_WEIGHTS["author"]),
        (Q(advisor__name__icontains=query), RELEVANCE_WEIGHTS["advisor"]),
        (_course_match(query), RELEVANCE_WEIGHTS["course"]),
        (Q(abstract__icontains=query), RELEVANCE_WEIGHTS["abstract"]),
    ]
    score: Any = Value(0)
    for condition, weight in conditions:
        score = score + Case(When(condition, then=Value(weight)), default=Value(0), output_field=IntegerField())
    return score


def search_public_theses(filters: PublicSearchFilters) -> QuerySet[Thesis]:
    """
    Apply public search filters and ordering to published theses.

    Text filters are combined with AND; author and keyword matches use EXISTS so
    the result has no duplicated rows and needs no ``DISTINCT``.

    Args:
        filters: Validated search parameters.

    Returns:
        QuerySet[Thesis]: Ordered public queryset with list relations prefetched.
    """
    queryset = public_theses_queryset().annotate(repository_year=defense_year_expression())
    if filters.query:
        queryset = queryset.filter(_free_text_filter(filters.query))
    if filters.title:
        queryset = queryset.filter(title__icontains=filters.title)
    if filters.author:
        queryset = queryset.filter(_author_name_match(filters.author))
    if filters.advisor:
        queryset = queryset.filter(advisor__name__icontains=filters.advisor)
    if filters.course:
        queryset = queryset.filter(_course_match(filters.course))
    for keyword in filters.keywords:
        keyword_links = ThesisKeyword.objects.filter(thesis=OuterRef("pk"), keyword__normalized_name=keyword)
        queryset = queryset.filter(Exists(keyword_links))
    if filters.course_id:
        queryset = queryset.filter(course_id=filters.course_id)
    if filters.advisor_id:
        queryset = queryset.filter(advisor_id=filters.advisor_id)
    if filters.year:
        queryset = queryset.filter(repository_year=filters.year)
    ordering = filters.effective_ordering
    if ordering == ORDERING_TITLE:
        return queryset.order_by(Lower("title"), "-published_at", "pk")
    if ordering == ORDERING_RELEVANCE:
        queryset = queryset.annotate(relevance=_relevance_expression(filters.query))
        return queryset.order_by("-relevance", F("defense_date").desc(nulls_last=True), "-published_at", "pk")
    return queryset.order_by(F("defense_date").desc(nulls_last=True), "-published_at", "pk")


def public_person_payload(user: User | None) -> dict[str, Any] | None:
    """
    Serialize a person for anonymous payloads (no e-mail).

    Args:
        user: Linked user or ``None``.

    Returns:
        dict[str, Any] | None: ``id`` and ``name``, or ``None`` without a user.
    """
    if user is None:
        return None
    return {"id": str(user.pk), "name": user.name or ""}


def _public_authors(thesis: Thesis) -> list[dict[str, Any]]:
    """
    Serialize the thesis authors for anonymous payloads.

    Args:
        thesis: Thesis with ``authors__author`` prefetched.

    Returns:
        list[dict[str, Any]]: Authors in position order.
    """
    authors: list[dict[str, Any]] = []
    for link in thesis.authors.all():
        person = public_person_payload(link.author)
        authors.append(
            {
                "id": str(link.pk),
                "position": link.position,
                "isMainAuthor": link.is_main_author,
                "name": person["name"] if person else "",
                "user": person,
            }
        )
    return authors


def _public_main_author(authors: list[dict[str, Any]]) -> dict[str, Any] | None:
    """
    Return the public payload of the main author.

    Args:
        authors: Serialized public authors.

    Returns:
        dict[str, Any] | None: ``id`` (``None`` for authors without account) and ``name``.
    """
    for author in authors:
        if author["isMainAuthor"]:
            user = author["user"]
            return {"id": user["id"] if user else None, "name": author["name"]}
    return None


def _current_file(thesis: Thesis) -> ThesisFile | None:
    """
    Return the current file from the prefetched ``files`` relation.

    Args:
        thesis: Thesis with ``files__stored_file`` prefetched.

    Returns:
        ThesisFile | None: Current version.
    """
    for thesis_file in thesis.files.all():
        if thesis_file.is_current:
            return thesis_file
    return None


def _public_download_urls(thesis: Thesis) -> dict[str, str]:
    """
    Build the anonymous download and preview URLs of a thesis.

    Args:
        thesis: Published thesis.

    Returns:
        dict[str, str]: ``downloadUrl`` and ``previewUrl``.
    """
    download_url = f"/api/v1/public/theses/{thesis.pk}/download"
    return {"downloadUrl": download_url, "previewUrl": f"{download_url}?disposition=inline"}


def _public_common_payload(thesis: Thesis) -> dict[str, Any]:
    """
    Build the fields shared by the public list and detail payloads.

    Args:
        thesis: Published thesis with list relations prefetched.

    Returns:
        dict[str, Any]: Common public fields.
    """
    authors = _public_authors(thesis)
    return {
        "id": str(thesis.pk),
        "referenceCode": thesis.reference_code,
        "type": thesis.type,
        "typeLabel": ThesisType(thesis.type).label,
        "title": thesis.title,
        "abstract": thesis.abstract,
        "journal": thesis.journal,
        "status": thesis.status,
        "statusLabel": ThesisStatus(thesis.status).label,
        "statusDescription": STATUS_DESCRIPTIONS.get(thesis.status, ""),
        "progressStep": WORKFLOW_STEPS.get(thesis.status, 1),
        "isPublic": thesis.is_public,
        "course": course_brief(thesis.course),
        "advisor": public_person_payload(thesis.advisor),
        "student": _public_main_author(authors),
        "authors": authors,
        "authorNames": [author["name"] for author in authors if author["name"]],
        "keywords": [link.keyword.name for link in thesis.keyword_links.all()],
        "submittedAt": thesis.submitted_at.isoformat() if thesis.submitted_at else None,
        "advisorApprovedAt": thesis.advisor_approved_at.isoformat() if thesis.advisor_approved_at else None,
        "publishedAt": thesis.published_at.isoformat() if thesis.published_at else None,
        "defenseDate": thesis.defense_date.isoformat() if thesis.defense_date else None,
        "defenseYear": thesis.defense_year,
        "updatedAt": thesis.updated_at.isoformat(),
        "permissions": thesis_permissions(thesis, None),
    }


def public_thesis_list_payload(thesis: Thesis) -> dict[str, Any]:
    """
    Serialize a published thesis for the public search results.

    Keeps the keys of the former list payload (``thesis_list_payload``) so the
    contract stays additive, but with public URLs and without e-mails.

    Args:
        thesis: Published thesis with list relations prefetched.

    Returns:
        dict[str, Any]: Public summary.
    """
    payload = _public_common_payload(thesis)
    current_file = _current_file(thesis)
    file_payload: dict[str, Any] | None = None
    if current_file is not None:
        file_payload = {
            "id": str(current_file.pk),
            "versionNumber": current_file.version_number,
            "originalFilename": current_file.stored_file.original_filename,
            "sizeBytes": current_file.stored_file.size_bytes,
            "sha256": current_file.stored_file.sha256_hex,
            "uploadedAt": current_file.uploaded_at.isoformat(),
            **_public_download_urls(thesis),
        }
    payload["currentVersion"] = current_file.version_number if current_file else None
    payload["currentFile"] = file_payload
    payload["pendingReview"] = False
    return payload


def public_thesis_detail_payload(thesis: Thesis) -> dict[str, Any]:
    """
    Serialize a published thesis for the public detail page.

    Exposes the SHA-256 of the published PDF (``integrity``) so readers can check
    a downloaded copy, plus the hash of every previous version (no download).

    Args:
        thesis: Published thesis with list relations prefetched.

    Returns:
        dict[str, Any]: Public detail.
    """
    payload = _public_common_payload(thesis)
    current_file = _current_file(thesis)
    current_payload: dict[str, Any] | None = None
    integrity: dict[str, Any] | None = None
    if current_file is not None:
        current_payload = thesis_file_payload(current_file)
        current_payload.update(_public_download_urls(thesis))
        integrity = {
            "algorithm": "SHA-256",
            "sha256": current_file.stored_file.sha256_hex,
            "sizeBytes": current_file.stored_file.size_bytes,
            "versionNumber": current_file.version_number,
            "hashVerifiedAt": current_file.hash_verified_at.isoformat() if current_file.hash_verified_at else None,
        }
    versions = sorted(thesis.files.all(), key=lambda item: item.version_number, reverse=True)
    payload["currentFile"] = current_payload
    payload["integrity"] = integrity
    payload["fileVersions"] = [
        {
            "versionNumber": version.version_number,
            "isCurrent": version.is_current,
            "sha256": version.stored_file.sha256_hex,
            "sizeBytes": version.stored_file.size_bytes,
            "uploadedAt": version.uploaded_at.isoformat(),
        }
        for version in versions
    ]
    payload["createdAt"] = thesis.created_at.isoformat()
    return payload


def public_repository_filters_payload() -> dict[str, list[dict[str, Any]]]:
    """
    Build the filter options present in published theses, with counts.

    Every list is aggregated in the database (one query each), so the cost does
    not grow with the number of theses on the page.

    Returns:
        dict[str, list[dict[str, Any]]]: ``courses``, ``advisors``, ``years`` and ``keywords``.
    """
    base = published_theses()
    course_rows = base.values("course_id", "course__name", "course__code").annotate(total=Count("id")).order_by("course__name")
    advisor_rows = base.values("advisor_id", "advisor__name").annotate(total=Count("id")).order_by("advisor__name")
    year_rows = base.annotate(repository_year=defense_year_expression()).values("repository_year").annotate(total=Count("id")).order_by("-repository_year")
    keyword_links = ThesisKeyword.objects.filter(thesis__in=base.values("pk"))
    keyword_rows = keyword_links.values("keyword__name", "keyword__normalized_name").annotate(total=Count("thesis_id", distinct=True)).order_by("-total", "keyword__name")[:PUBLIC_KEYWORDS_LIMIT]
    return {
        "courses": [{"id": str(row["course_id"]), "name": row["course__name"], "code": row["course__code"], "count": row["total"]} for row in course_rows],
        "advisors": [{"id": str(row["advisor_id"]), "name": row["advisor__name"] or "", "count": row["total"]} for row in advisor_rows],
        "years": [{"year": row["repository_year"], "count": row["total"]} for row in year_rows if row["repository_year"] is not None],
        "keywords": [{"name": row["keyword__name"], "value": row["keyword__normalized_name"], "count": row["total"]} for row in keyword_rows],
    }


def public_courses_payload(only_with_published: bool = False) -> list[dict[str, Any]]:
    """
    List courses for anonymous visitors with their number of published theses.

    Args:
        only_with_published: Whether to drop courses without published theses.

    Returns:
        list[dict[str, Any]]: Courses ordered by name.
    """
    published_filter = Q(theses__in=published_theses().values("pk"))
    courses = Course.objects.select_related("department").annotate(published_count=Count("theses", filter=published_filter)).order_by("name")
    if only_with_published:
        courses = courses.filter(published_count__gt=0)
    return [
        {
            "id": str(course.pk),
            "name": course.name,
            "code": course.code,
            "department": {"id": str(course.department_id), "name": course.department.name},
            "publishedThesesCount": course.published_count,
        }
        for course in courses
    ]
