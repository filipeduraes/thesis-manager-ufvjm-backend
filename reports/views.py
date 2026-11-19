"""
Read-only reporting endpoints for coordinator and administrator dashboards.
"""

from __future__ import annotations

import logging

from django.core.cache import cache
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsCoordinatorOrAdmin
from reports.serializers import OverviewFiltersSerializer
from reports.services import REPORT_CACHE_TTL_SECONDS, build_overview, overview_cache_key, resolve_report_scope

logger = logging.getLogger(__name__)


class ReportsOverviewView(APIView):
    """
    ``GET /api/v1/reports/overview``: theses by course, advisor and defense year,
    approval rate and average time between submission and defense.

    Administrators see every course; coordinators see only the courses they
    coordinate. Advisors, students and external sessions receive 403.
    """

    permission_classes = [IsCoordinatorOrAdmin]
    throttle_scope = "reports"

    def get(self, request: Request) -> Response:
        """
        Return the overview report for the caller's scope and filters.

        Args:
            request: HTTP request with optional ``courseId``, ``advisorId``,
                ``year`` and ``scope=coordinated``.

        Returns:
            Response: Report payload (cached for a minute per scope and filters),
            or 400 with field errors.
        """
        filters_serializer = OverviewFiltersSerializer(data=request.query_params)
        if not filters_serializer.is_valid():
            return Response(filters_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        filters = filters_serializer.to_filters()
        scope = resolve_report_scope(request.user, filters)
        cache_key = overview_cache_key(scope, filters)
        cached_payload = cache.get(cache_key)
        if cached_payload is not None:
            return Response(cached_payload)
        payload = build_overview(scope, filters)
        cache.set(cache_key, payload, REPORT_CACHE_TTL_SECONDS)
        logger.debug("Reports overview computed: scope=%s", scope.code)
        return Response(payload, status=status.HTTP_200_OK)
