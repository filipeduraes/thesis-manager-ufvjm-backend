"""
Read-only user directory lists derived from the assignment's endpoint contract.

``GET /api/v1/public/advisors`` is public and searchable (PARTE 2, lista de
orientadores), while any listing beyond advisors — students, coordinators,
administrators — is restricted to the COORDINATOR and ADMIN roles. Both lists
return minimal user payloads (no academic profile data beyond identity).
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING

from django.core.cache import cache
from django.db.models import Q
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

if TYPE_CHECKING:
    from django.db.models import QuerySet
    from rest_framework.request import Request

from accounts.models import ProfileRole, User
from accounts.permissions import IsCoordinatorOrAdmin
from accounts.services import google_social_accounts_prefetch, user_avatar_url

# TTL curto: a base muda raramente e a consulta é quente (autocompletar);
# o cache evita varreduras repetidas com muitas digitações por segundo.
DIRECTORY_CACHE_TTL_SECONDS = 60
DIRECTORY_DEFAULT_PAGE_SIZE = 20
DIRECTORY_MAX_PAGE_SIZE = 50
DIRECTORY_LIST_CAP = 5000

ROLE_FILTERS: dict[str, tuple[str, ...]] = {
    ProfileRole.STUDENT.value: (ProfileRole.STUDENT,),
    ProfileRole.ADVISOR.value: (ProfileRole.ADVISOR,),
    ProfileRole.COORDINATOR.value: (ProfileRole.COORDINATOR,),
    ProfileRole.ADMIN.value: (ProfileRole.ADMIN,),
}


def _users_with_roles(role_codes: tuple[str, ...] | None, query: str, include_email_search: bool = True) -> QuerySet[User]:
    """
    Build the filtered, ordered queryset for directory searches.

    Args:
        role_codes: Active profile roles to restrict the list to (``None`` means
            no role filter).
        query: Trimmed search fragment (matched against name and, optionally,
            e-mail prefix), or the empty string for no filter.
        include_email_search: When ``False``, only name is searched (used for
            the public advisors list to avoid e-mail enumeration).

    Returns:
        QuerySet[User]: Ordered queryset of active users.
    """
    base = User.objects.filter(is_active=True)
    if role_codes:
        base = base.filter(
            profile_roles__role__in=role_codes,
            profile_roles__revoked_at__isnull=True,
        )
    if query:
        condition = Q(name__icontains=query)
        if include_email_search:
            condition |= Q(email__startswith=query)
        base = base.filter(condition)
    return (
        base.distinct()
        .only("id", "name", "email")
        .prefetch_related(google_social_accounts_prefetch())
        .order_by("name", "email")
    )


def _user_public_payload(user: User) -> dict[str, object]:
    """
    Serialize a public directory entry.

    Args:
        user: Institutional user with avatars prefetched.

    Returns:
        dict[str, object]: ``id``, ``name`` and ``avatarUrl`` (no e-mail).
    """
    return {
        "id": str(user.pk),
        "name": user.name or user.email,
        "avatarUrl": user_avatar_url(user),
    }


def _user_directory_payload(user: User) -> dict[str, object]:
    """
    Serialize a restricted directory entry including the e-mail.

    Args:
        user: Institutional user with avatars prefetched and roles active.

    Returns:
        dict[str, object]: ``id``, ``name``, ``email``, ``avatarUrl`` and the
            user's ``roles``.
    """
    roles = [role for role in user.active_profile_roles().values_list("role", flat=True)]
    return {
        "id": str(user.pk),
        "name": user.name or user.email,
        "email": user.email,
        "avatarUrl": user_avatar_url(user),
        "roles": roles,
    }


def _paginate(request: Request, queryset: QuerySet[User]) -> tuple[list[User], int, int, int, int]:
    """
    Slice a directory queryset for the requested page.

    Args:
        request: HTTP request carrying ``page``/``pageSize``.
        queryset: Ordered, deduplicated directory queryset.

    Returns:
        tuple[list[User], int, int, int, int]: Items on this page, page, page
            size, total count and total pages.
    """
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(DIRECTORY_MAX_PAGE_SIZE, max(1, int(request.query_params.get("pageSize") or DIRECTORY_DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        page_size = DIRECTORY_DEFAULT_PAGE_SIZE
    total = queryset[:DIRECTORY_LIST_CAP + 1].count()
    if total > DIRECTORY_LIST_CAP:
        total = DIRECTORY_LIST_CAP
    total_pages = math.ceil(total / page_size) if total else 1
    offset = (page - 1) * page_size
    items = list(queryset[offset:offset + page_size])
    return items, page, page_size, total, total_pages


def _pagination_cache_keys(request: Request) -> tuple[int, int]:
    """
    Normalize the pagination parameters used in directory cache keys.

    Args:
        request: HTTP request carrying ``page``/``pageSize``.

    Returns:
        tuple[int, int]: Effective page and page size, matching ``_paginate``.
    """
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(DIRECTORY_MAX_PAGE_SIZE, max(1, int(request.query_params.get("pageSize") or DIRECTORY_DEFAULT_PAGE_SIZE)))
    except (TypeError, ValueError):
        page_size = DIRECTORY_DEFAULT_PAGE_SIZE
    return page, page_size


def _paginated_body(items: list[dict[str, object]], page: int, page_size: int, total: int, total_pages: int) -> dict[str, object]:
    """
    Build the standard paginated directory envelope.

    Args:
        items: Rows on this page.
        page: Current page.
        page_size: Page size in effect.
        total: Total matching users.
        total_pages: Total number of pages.

    Returns:
        dict[str, object]: Pagination envelope plus results.
    """
    return {
        "count": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "lastPageCount": len(items) or page_size,
        "results": items,
    }


class PublicAdvisorsListView(APIView):
    """
    Public, searchable list of the institution's advisors.

    The assignment requires this endpoint to be anonymous. Only display identity
    is exposed; e-mail addresses never appear on the public surface.
    """

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_scope = "session"

    def get(self, request: Request) -> Response:
        """
        Return a paginated, searchable list of active advisors.

        Args:
            request: HTTP request with optional ``q``, ``page`` and ``pageSize``.

        Returns:
            Response: Pagination envelope with the advisors in view.
        """
        query = (request.query_params.get("q") or "").strip().lower()
        page_key, size_key = _pagination_cache_keys(request)
        cache_key = f"api:public_advisors:{query}:{page_key}:{size_key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
        queryset = _users_with_roles((ProfileRole.ADVISOR,), query, include_email_search=False)
        items, page, page_size, total, total_pages = _paginate(request, queryset)
        payload = _paginated_body([_user_public_payload(user) for user in items], page, page_size, total, total_pages)
        cache.set(cache_key, payload, DIRECTORY_CACHE_TTL_SECONDS)
        return Response(payload, status=status.HTTP_200_OK)


class UserDirectoryListView(APIView):
    """
    Restricted directory of students, coordinators and administrators.

    Callers must hold the COORDINATOR or ADMIN role (or be staff/superuser).
    The e-mail is included; the list is paginated and cacheable. Use ``role``
    to restrict to ``STUDENT``, ``ADVISOR``, ``COORDINATOR`` or ``ADMIN``, and
    ``q`` to search by name or e-mail prefix.
    """

    permission_classes = [IsCoordinatorOrAdmin]
    throttle_scope = "session"

    def get(self, request: Request) -> Response:
        """
        Return a paginated, searchable, role-filtered user list.

        Args:
            request: HTTP request with optional ``q``, ``role``, ``page`` and
                ``pageSize``.

        Returns:
            Response: Pagination envelope with the matching users.
        """
        query = (request.query_params.get("q") or "").strip().lower()
        role = (request.query_params.get("role") or "").strip().upper()
        if role and role not in ROLE_FILTERS:
            return Response({"detail": "Valor de role inválido."}, status=status.HTTP_400_BAD_REQUEST)
        roles = ROLE_FILTERS.get(role) if role else None
        page_key, size_key = _pagination_cache_keys(request)
        cache_key = f"api:user_directory:{roles}:{query}:{page_key}:{size_key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
        queryset = _users_with_roles(roles, query)
        items, page, page_size, total, total_pages = _paginate(request, queryset)
        payload = _paginated_body([_user_directory_payload(user) for user in items], page, page_size, total, total_pages)
        cache.set(cache_key, payload, DIRECTORY_CACHE_TTL_SECONDS)
        return Response(payload, status=status.HTTP_200_OK)
