"""
Read-only reporting endpoints for coordinator and administrator dashboards.
"""

from __future__ import annotations

import logging

from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.renderers import BaseRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.audit_identifiers import EntityType
from accounts.audit_entry import AuditEntry
from accounts.models import AuditAction
from accounts.permissions import IsCoordinatorOrAdmin
from accounts.services import record_audit
from reports.pdf import render_overview_pdf, report_pdf_filename
from reports.serializers import OverviewFiltersSerializer
from reports.services import ReportFilters, get_overview_payload, resolve_report_scope

logger = logging.getLogger(__name__)

PDF_CONTENT_TYPE = "application/pdf"


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
        payload = get_overview_payload(scope, filters)
        return Response(payload, status=status.HTTP_200_OK)


def _export_audit_metadata(scope_code: str, filters: ReportFilters) -> dict[str, str | int | None]:
    """
    Build the audit metadata of a report export: identifiers only, no names.

    Args:
        scope_code: Resolved scope (``ALL`` or ``COORDINATED_COURSES``).
        filters: Validated filters.

    Returns:
        dict[str, str | int | None]: Metadata stored in ``audit_log``.
    """
    return {
        "format": "pdf",
        "scope": scope_code,
        "courseId": str(filters.course_id) if filters.course_id else None,
        "advisorId": str(filters.advisor_id) if filters.advisor_id else None,
        "year": filters.year,
    }


class ReportsOverviewPdfView(APIView):
    """
    ``GET /api/v1/reports/overview/pdf``: the overview report as a PDF download.

    Same filters, permissions, scope and cached payload as ``ReportsOverviewView``,
    with its own throttle scope (``reports_export``) so exports do not consume the
    dashboard quota. Every export is recorded in ``audit_log`` as DOWNLOAD.
    """

    permission_classes = [IsCoordinatorOrAdmin]
    throttle_scope = "reports_export"

    def perform_content_negotiation(self, request: Request, force: bool = False) -> tuple[BaseRenderer, str]:
        """
        Negotiate leniently so errors are always JSON.

        A client that sends only ``Accept: application/pdf`` would otherwise get
        406 before permissions run; the PDF itself bypasses renderers because it
        is returned as a plain ``HttpResponse``.

        Args:
            request: Incoming request.
            force: Ignored; negotiation is always forced.

        Returns:
            tuple[BaseRenderer, str]: Renderer and media type for error responses.
        """
        return super().perform_content_negotiation(request, force=True)

    def get(self, request: Request) -> HttpResponse:
        """
        Render the caller's overview report as a PDF attachment.

        Args:
            request: HTTP request with optional ``courseId``, ``advisorId``,
                ``year`` and ``scope=coordinated``.

        Returns:
            HttpResponse: ``application/pdf`` attachment named
            ``relatorio-tcc-AAAAMMDD-HHMM.pdf``, or 400 (JSON) with field errors.
        """
        filters_serializer = OverviewFiltersSerializer(data=request.query_params)
        if not filters_serializer.is_valid():
            return Response(filters_serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        filters = filters_serializer.to_filters()
        scope = resolve_report_scope(request.user, filters)
        payload = get_overview_payload(scope, filters)
        generated_at = timezone.now()
        pdf_content = render_overview_pdf(payload, generated_at)
        record_audit(AuditEntry(
            action=AuditAction.DOWNLOAD,
            entity_type=EntityType.REPORT_OVERVIEW,
            entity_id="overview",
            request=request,
            metadata=_export_audit_metadata(scope.code, filters),
        ))
        logger.info("Reports overview PDF exported: scope=%s size=%d", scope.code, len(pdf_content))
        filename = report_pdf_filename(generated_at)
        response = HttpResponse(pdf_content, content_type=PDF_CONTENT_TYPE)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        response["Cache-Control"] = "no-store"
        return response
