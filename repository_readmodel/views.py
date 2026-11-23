"""
Public HTTP API served entirely from the repository service's read model.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from repository_readmodel.models import PublicAdvisor, PublicCourse, PublicKeyword, PublicThesis
from theses.repository import ORDERING_CHOICES, PublicSearchFilters
from theses.repository_cache import cached_payload, courses_cache_key, detail_cache_key, filters_cache_key, search_cache_key
from theses.serializers import PublicThesisSearchSerializer
from thesismanagerbackend.throttling import ClientIpScopedRateThrottle


class RepositoryPagination:
    """Small pagination envelope compatible with the existing public API."""

    page_size = 10
    max_page_size = 50


def _paginate(request, queryset) -> tuple[list[PublicThesis], int, int, int, int]:
    """Slice a repository queryset and return pagination metadata."""
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        size = min(RepositoryPagination.max_page_size, max(1, int(request.query_params.get("pageSize") or RepositoryPagination.page_size)))
    except (TypeError, ValueError):
        size = RepositoryPagination.page_size
    total = queryset.count()
    total_pages = max(1, (total + size - 1) // size)
    items = list(queryset[(page - 1) * size:page * size])
    return items, page, size, total, total_pages


def _envelope(items: list[dict[str, Any]], page: int, size: int, total: int, total_pages: int) -> dict[str, Any]:
    """Build the standard pagination envelope."""
    return {"count": total, "page": page, "pageSize": size, "totalPages": total_pages, "lastPageCount": len(items) or size, "results": items}


def _list_payload(item: PublicThesis) -> dict[str, Any]:
    """Serialize a public thesis summary from the read model."""
    return {
        "id": str(item.pk),
        "referenceCode": item.reference_code,
        "title": item.title,
        "abstract": item.abstract,
        "keywords": item.keywords,
        "authorNames": item.author_names,
        "advisor": {"id": str(item.advisor_id), "name": item.advisor.name} if item.advisor_id else None,
        "course": {"id": str(item.course_id), "name": item.course.name, "code": item.course.code} if item.course_id else None,
        "defenseDate": item.defense_date.isoformat() if item.defense_date else None,
        "defenseYear": item.defense_year,
        "publishedAt": item.published_at.isoformat() if item.published_at else None,
        "currentFile": {
            "sha256": item.pdf_sha256,
            "sizeBytes": item.pdf_size_bytes,
            "versionNumber": item.pdf_version,
            "downloadUrl": f"/api/v1/public/theses/{item.pk}/download",
        } if item.pdf_sha256 else None,
    }


def _detail_payload(item: PublicThesis) -> dict[str, Any]:
    """Serialize the full public detail from the read model."""
    payload = _list_payload(item)
    payload.update({
        "origin": item.origin,
        "fileVersions": item.file_versions,
        "integrity": {"sha256": item.pdf_sha256, "algorithm": "SHA-256"} if item.pdf_sha256 else None,
    })
    return payload


def _filtered(filters: PublicSearchFilters) -> QuerySet[PublicThesis]:
    """Apply the public search filters to the read model."""
    queryset = PublicThesis.objects.all()
    for term in filters.query.split():
        queryset = queryset.filter(search_text__icontains=term)
    if filters.title:
        queryset = queryset.filter(search_text__icontains=f"titulo::{filters.title.casefold()}")
    if filters.author:
        queryset = queryset.filter(author_names__icontains=filters.author)
    if filters.advisor:
        queryset = queryset.filter(advisor__name__icontains=filters.advisor)
    if filters.course:
        queryset = queryset.filter(Q(course__name__icontains=filters.course) | Q(course__code__icontains=filters.course))
    for keyword in filters.keywords:
        queryset = queryset.filter(keywords__icontains=keyword)
    if filters.course_id:
        queryset = queryset.filter(course_id=filters.course_id)
    if filters.advisor_id:
        queryset = queryset.filter(advisor_id=filters.advisor_id)
    if filters.year:
        queryset = queryset.filter(defense_year=filters.year)
    if filters.ordering == "title":
        return queryset.order_by("title")
    return queryset.order_by("-published_at")


class ReadModelBaseView(APIView):
    """Anonymous endpoint of the repository service."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "public_repository"


class PublicThesisListReadModelView(ReadModelBaseView):
    """Search published theses in the repository read model."""

    def get(self, request) -> Response:
        """Return a cached paginated page of published theses."""
        serializer = PublicThesisSearchSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        filters = serializer.to_filters()
        page_key = str(request.query_params.get("page") or "1")
        size_key = str(request.query_params.get("pageSize") or "")
        key = search_cache_key(filters, page_key, size_key)

        def build() -> dict[str, Any]:
            items, page, size, total, total_pages = _paginate(request, _filtered(filters))
            body = _envelope([_list_payload(item) for item in items], page, size, total, total_pages)
            body["ordering"] = filters.effective_ordering
            return body

        payload, hit = cached_payload(key, build)
        response = Response(payload)
        response["X-Repository-Cache"] = "HIT" if hit else "MISS"
        return response


class PublicThesisDetailReadModelView(ReadModelBaseView):
    """Return the public detail of one projected thesis."""

    def get(self, request, thesis_id) -> Response:
        """Return the cached public detail."""
        def build() -> dict[str, Any]:
            item = PublicThesis.objects.filter(pk=thesis_id).first()
            if item is None:
                return {"detail": "Monografia não encontrada."}
            return {"thesis": _detail_payload(item)}

        payload, _ = cached_payload(detail_cache_key(thesis_id), build)
        return Response(payload)


class PublicFiltersReadModelView(ReadModelBaseView):
    """List dynamic filter options present in the projection."""

    def get(self, request) -> Response:
        """Return courses, advisors, years and keywords with counts."""
        def build() -> dict[str, Any]:
            courses = list(PublicCourse.objects.annotate(total=Count("theses")).filter(total__gt=0).order_by("name").values("id", "name", "code", "total"))
            advisors = list(PublicAdvisor.objects.annotate(total=Count("theses")).filter(total__gt=0).order_by("name").values("id", "name", "total"))
            years = list(PublicThesis.objects.exclude(defense_year__isnull=True).values("defense_year").annotate(total=Count("id")).order_by("-defense_year"))
            keywords = list(PublicKeyword.objects.order_by("name").values("name", "value", "published_count"))
            return {
                "courses": [{"id": str(c["id"]), "name": c["name"], "code": c["code"], "count": c["total"]} for c in courses],
                "advisors": [{"id": str(a["id"]), "name": a["name"], "count": a["total"]} for a in advisors],
                "years": [{"year": y["defense_year"], "count": y["total"]} for y in years],
                "keywords": [{"name": k["name"], "value": k["value"], "count": k["published_count"]} for k in keywords],
            }

        payload, _ = cached_payload(filters_cache_key(), build)
        return Response(payload)


class PublicCoursesReadModelView(ReadModelBaseView):
    """List courses that have published theses."""

    def get(self, request) -> Response:
        """Return public courses with their published counts."""
        def build() -> dict[str, Any]:
            queryset = PublicCourse.objects.annotate(total=Count("theses")).filter(total__gt=0).order_by("name")
            results = [{"id": str(c.pk), "name": c.name, "code": c.code, "publishedThesesCount": c.total} for c in queryset]
            return {"count": len(results), "results": results}

        payload, _ = cached_payload(courses_cache_key(), build)
        return Response(payload)
