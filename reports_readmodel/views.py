"""HTTP API of the reporting service, backed by its own read model."""

from __future__ import annotations

from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from reports_readmodel.services import get_overview_payload


class CanReadReports(BasePermission):
    """Allow coordinators, administrators and staff to read reports."""

    message = "Acesso restrito à coordenação e administração."

    def has_permission(self, request, view) -> bool:
        """Check the institutional role of the caller."""
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated or not user.is_active:
            return False
        return bool(
            getattr(user, "is_staff", False)
            or user.has_active_role("ADMIN")
            or user.has_active_role("COORDINATOR")
        )


class OverviewReadModelView(APIView):
    """Return the overview computed from the reporting database."""

    permission_classes = [CanReadReports]
    throttle_scope = "reports"

    def get(self, request) -> Response:
        """Return aggregate indicators with validated filters."""
        params = request.query_params
        filters: dict = {}
        if params.get("courseId"):
            from uuid import UUID

            try:
                filters["course_id"] = UUID(str(params["courseId"]))
            except ValueError:
                return Response({"detail": "Curso inválido."}, status=400)
        if params.get("advisorId"):
            from uuid import UUID

            try:
                filters["advisor_id"] = UUID(str(params["advisorId"]))
            except ValueError:
                return Response({"detail": "Orientador inválido."}, status=400)
        if params.get("year"):
            try:
                year = int(params["year"])
            except (TypeError, ValueError):
                return Response({"detail": "Ano inválido."}, status=400)
            if not 2000 <= year <= 2100:
                return Response({"detail": "Ano inválido."}, status=400)
            filters["year"] = year
        return Response(get_overview_payload(filters))
