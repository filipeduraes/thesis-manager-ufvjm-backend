"""
Serializers for the thesis submission and review API.
"""

from __future__ import annotations

import json
from dataclasses import replace

from django.conf import settings
from rest_framework import serializers

from academic.models import Course
from accounts.models import ProfileRole, User
from theses.models import AvailabilityStatus, CommitteeRole, DefenseModality, DefenseResult, DocumentType, ReviewDecision, ThesisType
from theses.repository import ORDERING_CHOICES, PublicSearchFilters
from theses.repository_management import LegacyAuthor, ThesisMetadataInput
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
    authors = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
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

    def validate_authors(self, value: list) -> list:
        """
        Limit the co-author list so the group has at most five students.

        Args:
            value: Identifiers of co-authors (the requester is the main author).

        Returns:
            list: Validated identifiers.

        Raises:
            serializers.ValidationError: When there are too many or repeated authors.
        """
        maximum = getattr(settings, "THESIS_MAX_AUTHORS", 5)
        if len(value) > maximum - 1:
            raise serializers.ValidationError(f"O grupo pode ter no máximo {maximum} alunos, incluindo você.")
        if len(set(value)) != len(value):
            raise serializers.ValidationError("O mesmo aluno foi informado mais de uma vez.")
        return value


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


class AvailabilityResponseSerializer(serializers.Serializer):
    """
    Validate a committee member's availability answer.
    """

    status = serializers.ChoiceField(choices=[AvailabilityStatus.CONFIRMED, AvailabilityStatus.DECLINED])
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)


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
    score = serializers.DecimalField(max_digits=4, decimal_places=2, min_value=0, max_value=10)
    notes = serializers.CharField(allow_blank=False, trim_whitespace=True)


class PublicThesisSearchSerializer(serializers.Serializer):
    """
    Validate the query parameters of the public repository search.

    Unknown parameters (``page``, ``pageSize``) are ignored here and handled by the
    paginator; invalid values return 400 instead of being silently dropped.
    """

    q = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    title = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    author = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    advisor = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")
    course = serializers.CharField(required=False, allow_blank=True, max_length=150, default="")
    keyword = serializers.ListField(child=serializers.CharField(max_length=50, allow_blank=True), required=False, default=list, max_length=10)
    courseId = serializers.UUIDField(required=False, allow_null=True, default=None)
    advisorId = serializers.UUIDField(required=False, allow_null=True, default=None)
    year = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=1900, max_value=2200)
    ordering = serializers.ChoiceField(choices=ORDERING_CHOICES, required=False, allow_blank=True, default="")

    def to_filters(self) -> PublicSearchFilters:
        """
        Convert the validated parameters into search filters.

        Returns:
            PublicSearchFilters: Normalized filters (texts collapsed, keywords case-folded).
        """
        data = self.validated_data
        keywords: list[str] = []
        for raw_keyword in data["keyword"]:
            normalized = " ".join(raw_keyword.split()).casefold()
            if normalized and normalized not in keywords:
                keywords.append(normalized)
        return PublicSearchFilters(
            query=" ".join(data["q"].split()),
            title=" ".join(data["title"].split()),
            author=" ".join(data["author"].split()),
            advisor=" ".join(data["advisor"].split()),
            course=" ".join(data["course"].split()),
            keywords=tuple(keywords),
            course_id=data["courseId"],
            advisor_id=data["advisorId"],
            year=data["year"],
            ordering=data["ordering"],
        )


class UnpublishThesisSerializer(serializers.Serializer):
    """
    Validate the reason given to remove a thesis from the public repository.
    """

    reason = serializers.CharField(max_length=1000, allow_blank=False, trim_whitespace=True)


class JsonListField(serializers.ListField):
    """
    List field that also accepts a JSON-encoded list.

    Multipart requests (needed for the PDF) cannot nest objects, so clients may
    send ``authors``/``keywords`` as a JSON string or as repeated form fields.
    """

    default_error_messages = {"invalid_json": "Envie uma lista JSON válida."}

    def to_internal_value(self, data):
        """
        Decode a JSON string before the regular list validation.

        Args:
            data: List, JSON string or single-item list holding a JSON string.

        Returns:
            list: Validated items.
        """
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], str) and data[0].lstrip().startswith("["):
            data = data[0]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                self.fail("invalid_json")
        return super().to_internal_value(data)


class RepositoryAuthorSerializer(serializers.Serializer):
    """
    Validate an author of a repository entry: a name and an optional user link.
    """

    name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    userId = serializers.PrimaryKeyRelatedField(source="user", queryset=User.objects.all(), required=False, allow_null=True, default=None)

    def validate(self, attrs: dict) -> dict:
        """
        Require a name or a linked user.

        Args:
            attrs: Validated attributes.

        Returns:
            dict: Validated attributes.

        Raises:
            serializers.ValidationError: When both are missing.
        """
        if not (attrs.get("name") or "").strip() and attrs.get("user") is None:
            raise serializers.ValidationError("Informe o nome do autor ou vincule um usuário.")
        return attrs


def _advisor_candidates():
    """
    Return users who have or had the advisor role.

    Returns:
        QuerySet[User]: Candidate advisors for repository entries.
    """
    return User.objects.filter(profile_roles__role=ProfileRole.ADVISOR).distinct()


class LegacyThesisSerializer(serializers.Serializer):
    """
    Validate the manual registration of a legacy (already approved) thesis.
    """

    title = serializers.CharField(max_length=500)
    abstract = serializers.CharField()
    keywords = JsonListField(child=serializers.CharField(max_length=50), allow_empty=False)
    authors = JsonListField(child=RepositoryAuthorSerializer(), allow_empty=False)
    advisorId = serializers.PrimaryKeyRelatedField(source="advisor", queryset=_advisor_candidates())
    courseId = serializers.PrimaryKeyRelatedField(source="course", queryset=Course.objects.all())
    defenseYear = serializers.IntegerField(source="defense_year", required=False, allow_null=True, default=None)
    defenseDate = serializers.DateField(source="defense_date", required=False, allow_null=True, default=None)
    result = serializers.ChoiceField(choices=[DefenseResult.APPROVED, DefenseResult.APPROVED_WITH_REMARKS])
    type = serializers.ChoiceField(choices=ThesisType.choices, required=False, default=ThesisType.MONOGRAPH)
    journal = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    file = serializers.FileField()

    def validate(self, attrs: dict) -> dict:
        """
        Derive the defense year from the date when only the date is sent.

        Args:
            attrs: Validated attributes.

        Returns:
            dict: Attributes with ``defense_year`` filled.

        Raises:
            serializers.ValidationError: When neither year nor date is sent.
        """
        if attrs.get("defense_year") is None:
            if attrs.get("defense_date") is None:
                raise serializers.ValidationError({"defenseYear": "Informe o ano ou a data de defesa."})
            attrs["defense_year"] = attrs["defense_date"].year
        return attrs

    def to_metadata(self) -> ThesisMetadataInput:
        """
        Build the service input from the validated data.

        Returns:
            ThesisMetadataInput: Repository metadata.
        """
        data = self.validated_data
        authors = [LegacyAuthor(name=item.get("name", ""), user=item.get("user")) for item in data["authors"]]
        return ThesisMetadataInput(
            title=data["title"],
            abstract=data["abstract"],
            keywords=data["keywords"],
            authors=authors,
            advisor=data["advisor"],
            course=data["course"],
            defense_year=data["defense_year"],
            defense_date=data["defense_date"],
            thesis_type=data["type"],
            journal=data["journal"],
        )


class PublishedThesisUpdateSerializer(serializers.Serializer):
    """
    Validate a partial edit of a published thesis' metadata.

    Every field is optional; omitted fields keep their current values.
    """

    title = serializers.CharField(max_length=500, required=False)
    abstract = serializers.CharField(required=False)
    keywords = JsonListField(child=serializers.CharField(max_length=50), required=False, allow_empty=False)
    authors = JsonListField(child=RepositoryAuthorSerializer(), required=False, allow_empty=False)
    advisorId = serializers.PrimaryKeyRelatedField(source="advisor", queryset=_advisor_candidates(), required=False)
    courseId = serializers.PrimaryKeyRelatedField(source="course", queryset=Course.objects.all(), required=False)
    defenseYear = serializers.IntegerField(source="defense_year", required=False)
    defenseDate = serializers.DateField(source="defense_date", required=False, allow_null=True)
    type = serializers.ChoiceField(choices=ThesisType.choices, required=False)
    journal = serializers.CharField(max_length=200, required=False, allow_blank=True)
    reason = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")

    def merge_into(self, base: ThesisMetadataInput) -> ThesisMetadataInput:
        """
        Overlay the sent fields on the current metadata.

        Sending only ``defenseDate`` also moves ``defenseYear`` to the date's year.

        Args:
            base: Current metadata of the thesis.

        Returns:
            ThesisMetadataInput: Complete metadata after the edit.
        """
        data = self.validated_data
        overrides: dict[str, object] = {}
        for source_name, target_name in (("title", "title"), ("abstract", "abstract"), ("keywords", "keywords"), ("advisor", "advisor"), ("course", "course"), ("defense_year", "defense_year"), ("defense_date", "defense_date"), ("type", "thesis_type"), ("journal", "journal")):
            if source_name in data:
                overrides[target_name] = data[source_name]
        if "authors" in data:
            overrides["authors"] = [LegacyAuthor(name=item.get("name", ""), user=item.get("user")) for item in data["authors"]]
        defense_date = overrides.get("defense_date")
        if "defense_year" not in overrides and defense_date is not None:
            overrides["defense_year"] = defense_date.year
        # Ano novo sem data nova: a data antiga de outro ano deixaria de valer,
        # então é descartada em vez de bloquear a edição.
        stale_date = base.defense_date is not None and base.defense_date.year != overrides.get("defense_year", base.defense_year)
        if "defense_date" not in overrides and stale_date:
            overrides["defense_date"] = None
        return replace(base, **overrides)


class PublishedFileSerializer(serializers.Serializer):
    """
    Validate the replacement PDF of a published thesis.
    """

    file = serializers.FileField()
    changeDescription = serializers.CharField(source="change_description", max_length=2000, required=False, allow_blank=True, default="")
