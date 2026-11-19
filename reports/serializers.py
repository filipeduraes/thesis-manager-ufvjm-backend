"""
Query-parameter validation for the reporting endpoints.
"""

from __future__ import annotations

from rest_framework import serializers

from reports.services import ReportFilters

MIN_REPORT_YEAR = 2000
MAX_REPORT_YEAR = 2100
COORDINATED_SCOPE_PARAM = "coordinated"


class OverviewFiltersSerializer(serializers.Serializer):
    """
    Validate ``courseId``, ``advisorId``, ``year`` and ``scope`` of the overview report.
    """

    courseId = serializers.UUIDField(required=False, error_messages={"invalid": "Curso inválido."})
    advisorId = serializers.UUIDField(required=False, error_messages={"invalid": "Orientador inválido."})
    year = serializers.IntegerField(
        required=False,
        min_value=MIN_REPORT_YEAR,
        max_value=MAX_REPORT_YEAR,
        error_messages={
            "invalid": "Ano inválido.",
            "min_value": "Ano inválido.",
            "max_value": "Ano inválido.",
        },
    )
    scope = serializers.ChoiceField(
        required=False,
        choices=[COORDINATED_SCOPE_PARAM],
        error_messages={"invalid_choice": "Escopo inválido. Use scope=coordinated ou omita o parâmetro."},
    )

    def to_filters(self) -> ReportFilters:
        """
        Convert validated query parameters into the service filters.

        Returns:
            ReportFilters: Filters consumed by ``build_overview``.
        """
        values = self.validated_data
        return ReportFilters(
            course_id=values.get("courseId"),
            advisor_id=values.get("advisorId"),
            year=values.get("year"),
            coordinated_only=values.get("scope") == COORDINATED_SCOPE_PARAM,
        )
