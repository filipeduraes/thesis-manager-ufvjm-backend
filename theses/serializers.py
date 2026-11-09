"""
Serializers for the thesis submission and review API.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from academic.models import Course
from accounts.models import User
from theses.models import CommitteeRole, DefenseModality, DefenseResult, DocumentType, ReviewDecision, ThesisType
from theses.services import available_advisors, normalize_keyword_names


class ThesisMetadataSerializer(serializers.Serializer):
    """
    Validate thesis metadata submitted by a student.
    """

    title = serializers.CharField(max_length=500)
    abstract = serializers.CharField()
    courseId = serializers.PrimaryKeyRelatedField(source="course", queryset=Course.objects.all())
    advisorId = serializers.PrimaryKeyRelatedField(source="advisor", queryset=available_advisors())
    keywords = serializers.ListField(
        child=serializers.CharField(max_length=50, allow_blank=False),
        allow_empty=False,
    )
    type = serializers.ChoiceField(choices=ThesisType.choices, default=ThesisType.MONOGRAPH, required=False)
    journal = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")

    def validate_title(self, value: str) -> str:
        """
        Normalize and validate the thesis title.

        Args:
            value: Raw title.

        Returns:
            str: Normalized title.

        Raises:
            serializers.ValidationError: When the title is empty.
        """
        normalized = " ".join((value or "").split())
        if not normalized:
            raise serializers.ValidationError("Informe o título da monografia.")
        return normalized

    def validate_abstract(self, value: str) -> str:
        """
        Validate the thesis abstract.

        Args:
            value: Raw abstract.

        Returns:
            str: Trimmed abstract.

        Raises:
            serializers.ValidationError: When the abstract is empty.
        """
        normalized = (value or "").strip()
        if not normalized:
            raise serializers.ValidationError("Informe o resumo da monografia.")
        return normalized

    def validate_keywords(self, value: list[str]) -> list[str]:
        """
        Normalize and validate the keyword list.

        Args:
            value: Raw keyword names.

        Returns:
            list[str]: Clean keyword names.

        Raises:
            serializers.ValidationError: When no valid keyword is provided.
        """
        cleaned = normalize_keyword_names(value)
        if not cleaned:
            raise serializers.ValidationError("Informe pelo menos uma palavra-chave.")
        maximum = getattr(settings, "THESIS_MAX_KEYWORDS", 10)
        if len(cleaned) > maximum:
            raise serializers.ValidationError(f"Informe no máximo {maximum} palavras-chave.")
        return cleaned


class ThesisReviewSerializer(serializers.Serializer):
    """
    Validate an advisor review payload.
    """

    decision = serializers.ChoiceField(choices=ReviewDecision.choices)
    comments = serializers.CharField(required=False, allow_blank=True, default="")
    recommendations = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs: dict) -> dict:
        """
        Require feedback when the advisor requests changes or rejects the version.

        Args:
            attrs: Validated attributes.

        Returns:
            dict: Validated attributes.

        Raises:
            serializers.ValidationError: When required feedback is missing.
        """
        decision = attrs["decision"]
        comments = (attrs.get("comments") or "").strip()
        if decision in {ReviewDecision.CHANGES_REQUESTED, ReviewDecision.REJECTED} and not comments:
            raise serializers.ValidationError(
                {"comments": "Descreva os ajustes ou o motivo da reprovação."}
            )
        attrs["comments"] = comments
        attrs["recommendations"] = (attrs.get("recommendations") or "").strip()
        return attrs


class DefenseResultSerializer(serializers.Serializer):
    """
    Validate a final defense result payload.
    """

    result = serializers.ChoiceField(choices=DefenseResult.choices)
    reason = serializers.CharField(required=False, allow_blank=True, default="")


class DocumentUploadSerializer(serializers.Serializer):
    """
    Validate a supplementary document upload.
    """

    type = serializers.ChoiceField(choices=DocumentType.choices)
    file = serializers.FileField()

    def validate_file(self, value):
        """
        Validate the document size before reading its content.

        Args:
            value: Uploaded file.

        Returns:
            UploadedFile: Original upload.

        Raises:
            serializers.ValidationError: When the file exceeds the configured limit.
        """
        maximum_mb = getattr(settings, "THESIS_MAX_DOCUMENT_SIZE_MB", 20)
        maximum_bytes = int(maximum_mb) * 1024 * 1024
        if value.size > maximum_bytes:
            raise serializers.ValidationError(f"O arquivo excede o limite de {maximum_mb} MB.")
        return value


class ScheduleDefenseSerializer(serializers.Serializer):
    """
    Validate a defense scheduling request.
    """

    scheduledDatetime = serializers.DateTimeField(source="scheduled_datetime")
    durationMinutes = serializers.IntegerField(source="duration_minutes", required=False, min_value=1, max_value=720, default=120)
    modality = serializers.ChoiceField(choices=DefenseModality.choices, required=False, default=DefenseModality.IN_PERSON)
    location = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class UpdateDefenseSerializer(serializers.Serializer):
    """
    Validate a partial defense update (all fields optional).
    """

    scheduledDatetime = serializers.DateTimeField(source="scheduled_datetime", required=False)
    durationMinutes = serializers.IntegerField(source="duration_minutes", required=False, min_value=1, max_value=720)
    modality = serializers.ChoiceField(choices=DefenseModality.choices, required=False)
    location = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)


class AddInstitutionalMemberSerializer(serializers.Serializer):
    """
    Validate an institutional committee member addition request.
    """

    role = serializers.ChoiceField(choices=CommitteeRole.choices)
    userId = serializers.PrimaryKeyRelatedField(source="user", queryset=User.objects.filter(is_active=True))
    conflictReason = serializers.CharField(source="conflict_reason", required=False, allow_blank=True, default="")


class AddExternalMemberSerializer(serializers.Serializer):
    """
    Validate an external committee member addition request.
    """

    role = serializers.ChoiceField(choices=CommitteeRole.choices)
    name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    institution = serializers.CharField(required=False, allow_blank=True, default="")
    conflictReason = serializers.CharField(source="conflict_reason", required=False, allow_blank=True, default="")


class SubmitEvaluationSerializer(serializers.Serializer):
    """
    Validate a committee member evaluation submission.
    """

    decision = serializers.ChoiceField(choices=DefenseResult.choices)
    score = serializers.DecimalField(max_digits=4, decimal_places=2, required=False, allow_null=True, min_value=0, max_value=10)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
