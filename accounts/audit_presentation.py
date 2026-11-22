"""
Human-readable presentation helpers for the administrative audit log.

The audit table stores stable codes and identifiers so that the hash chain stays
predictable.  This module turns those records into sentences and detail rows for
the administrative UI, using related records only when the stored metadata is not
enough (old entries, for example).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from django.db.models import Prefetch, TextChoices
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from accounts.audit_identifiers import (
    CATEGORY_LABELS,
    ENTITY_TYPE_LABELS,
    FIELD_LABELS,
    AuditCategory,
    AuditEvent,
    AuditField,
    EntityType,
)
from accounts.models import AuditAction, AuditLog, ExternalInvitation, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import ROLE_PRIORITY, role_label
from theses.models import AvailabilityStatus, CommitteeRole, DefenseResult, DefenseStatus, DocumentType, ReviewDecision, ThesisStatus

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
THESIS_TITLE_SUMMARY_LIMIT = 80

# Categories, entity types, fields and events are typed identifiers shared with
# the writers. The label maps are defined next to their enums.
CATEGORY_ACCESS = AuditCategory.ACCESS
CATEGORY_MANAGEMENT = AuditCategory.MANAGEMENT
CATEGORY_THESIS = AuditCategory.THESIS
CATEGORY_PUBLICATION = AuditCategory.PUBLICATION
CATEGORY_ERROR = AuditCategory.ERROR

def derive_audit_category(action: str, entity_type: EntityType, metadata: dict | None = None) -> AuditCategory:
    """
    Derive the audit category from the stored action, entity type and metadata.

    Args:
        action: Audit action code.
        entity_type: Typed affected entity type.
        metadata: Audit metadata mapping.

    Returns:
        AuditCategory: The derived verbosity category.
    """
    metadata = metadata if isinstance(metadata, dict) else {}
    event = str(metadata.get("event") or "").upper()
    if event == AuditEvent.AUTO_CLOSE_FAILED or "ERROR" in event:
        return CATEGORY_ERROR
    if action in {AuditAction.READ.value, AuditAction.DOWNLOAD.value}:
        return CATEGORY_ACCESS
    if action in {AuditAction.LOGIN.value, AuditAction.LOGOUT.value, AuditAction.LOGIN_FAILED.value, AuditAction.TOKEN_ACCESS.value}:
        return CATEGORY_ACCESS
    if action == AuditAction.PUBLISH.value or event == AuditEvent.PUBLISHED:
        return CATEGORY_PUBLICATION
    if entity_type == EntityType.REPORT_OVERVIEW:
        return CATEGORY_PUBLICATION
    if entity_type in {EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT, EntityType.DEFENSE}:
        return CATEGORY_THESIS
    return CATEGORY_MANAGEMENT

# Actions that belong to the "trace" (rastreamento) group kept for compatibility.
TRACE_ACTIONS = frozenset(
    {
        AuditAction.LOGIN.value,
        AuditAction.LOGOUT.value,
        AuditAction.READ.value,
        AuditAction.DOWNLOAD.value,
        AuditAction.TOKEN_ACCESS.value,
    }
)

ACTION_VERBS = {
    AuditAction.CREATE: "Criou",
    AuditAction.READ: "Consultou",
    AuditAction.UPDATE: "Atualizou",
    AuditAction.DELETE: "Excluiu",
    AuditAction.LOGIN: "Entrou no sistema",
    AuditAction.LOGOUT: "Saiu do sistema",
    AuditAction.LOGIN_FAILED: "Tentou entrar no sistema",
    AuditAction.UPLOAD: "Enviou",
    AuditAction.DOWNLOAD: "Baixou",
    AuditAction.REVIEW: "Revisou",
    AuditAction.SCHEDULE: "Agendou",
    AuditAction.EVALUATE: "Avaliou",
    AuditAction.PUBLISH: "Publicou",
    AuditAction.INVITE: "Convidou",
    AuditAction.TOKEN_ACCESS: "Acessou por token",
    AuditAction.APPROVE: "Aprovou",
    AuditAction.REJECT: "Rejeitou",
}

THESIS_ENTITY_TYPES = frozenset({EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT, EntityType.DEFENSE})

THESIS_DETAIL_SKIP_KEYS = frozenset({"title"})

SKIP_DETAIL_KEYS = frozenset(
    {
        AuditField.EVENT,
        AuditField.SESSION_KEY,
        AuditField.THESIS_ID,
        AuditField.THESIS_TITLE,
        AuditField.THESIS_REFERENCE,
        AuditField.REFERENCE_CODE,
        AuditField.MEMBER_ID,
        AuditField.DOCUMENT_ID,
        AuditField.INVITATION_ID,
        AuditField.PREVIOUS_INVITATION_ID,
        AuditField.REVIEW_ID,
        AuditField.DEFENSE_ID,
        AuditField.USER_ID,
    }
)

TRIGGER_LABELS = {
    "AUTO_AFTER_LAST_EVALUATION": "Automático após a última avaliação",
    "MANUAL": "Manual",
}

DISPOSITION_LABELS = {
    "inline": "Visualização",
    "attachment": "Download",
}

def choices_labels(enumeration: type[TextChoices]) -> dict[str, str]:
    """
    Build a code-to-label map from a Django ``TextChoices`` enumeration.

    Args:
        enumeration: TextChoices class used as the source of labels.

    Returns:
        dict[str, str]: Mapping of choice values to their labels.
    """
    return {code: label for code, label in enumeration.choices}


INVITATION_STATUS_LABELS = choices_labels(InvitationStatus)
DEFENSE_STATUS_LABELS = choices_labels(DefenseStatus)
AVAILABILITY_STATUS_LABELS = choices_labels(AvailabilityStatus)
THESIS_STATUS_LABELS = choices_labels(ThesisStatus)
DEFENSE_RESULT_LABELS = choices_labels(DefenseResult)
REVIEW_DECISION_LABELS = choices_labels(ReviewDecision)
COMMITTEE_ROLE_LABELS = choices_labels(CommitteeRole)
DOCUMENT_TYPE_LABELS = choices_labels(DocumentType)


def _uuid(value: Any) -> str | None:
    """
    Return a normalized UUID string when ``value`` is a valid identifier.

    Args:
        value: Raw value read from an audit record or metadata.

    Returns:
        str | None: Normalized UUID or ``None`` when the value is not an identifier.
    """
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _text(value: Any) -> str:
    """
    Return a stripped string representation of a value.

    Args:
        value: Value to normalize.

    Returns:
        str: Normalized text, or an empty string for missing values.
    """
    if value is None:
        return ""
    return str(value).strip()


def _truncate_text(value: Any, limit: int = THESIS_TITLE_SUMMARY_LIMIT) -> str:
    """
    Truncate a text at the last word boundary before the limit.

    Args:
        value: Text to shorten.
        limit: Maximum number of characters before the ellipsis.

    Returns:
        str: Shortened text ending in ``...`` when it was longer than the limit.
    """
    text = _text(value)
    if len(text) <= limit:
        return text
    candidate = text[:limit]
    last_space = candidate.rfind(" ")
    if last_space > 0:
        candidate = candidate[:last_space]
    candidate = candidate.rstrip(" ,.;:-")
    if not candidate:
        candidate = text[: max(limit - 3, 1)]
    return f"{candidate}..."


def _join_names(names: Iterable[str]) -> str:
    """
    Join names in a natural Portuguese list.

    Args:
        names: Names to join.

    Returns:
        str: ``A``, ``A e B`` or ``A, B e C``.
    """
    cleaned = [name for name in (_text(item) for item in names) if name]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} e {cleaned[1]}"
    return f"{', '.join(cleaned[:-1])} e {cleaned[-1]}"


def _format_score(value: Any) -> str:
    """
    Format a decimal score with a comma and no unnecessary trailing zeros.

    Args:
        value: Score read from metadata or the evaluation record.

    Returns:
        str: Formatted score, such as ``9`` or ``9,5``.
    """
    if value in (None, ""):
        return ""
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    text = f"{score:.2f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _format_size(value: Any) -> str:
    """
    Format a byte count using binary units.

    Args:
        value: Size in bytes.

    Returns:
        str: Human-readable size, such as ``1,2 MB``.
    """
    try:
        size = float(value)
    except (TypeError, ValueError):
        return str(value)
    units = ("B", "KB", "MB", "GB")
    unit = units[0]
    for candidate in units:
        unit = candidate
        if size < 1024 or candidate == units[-1]:
            break
        size /= 1024
    formatted = f"{size:.1f}".rstrip("0").rstrip(".")
    return f"{formatted.replace('.', ',')} {unit}"


def _format_datetime(value: Any) -> str:
    """
    Format an ISO timestamp in the São Paulo time zone.

    Args:
        value: Datetime instance or ISO string.

    Returns:
        str: Date and time in ``dd/mm/aaaa às HH:MM`` format.
    """
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = parse_datetime(value)
    if parsed is None:
        return _text(value)
    if timezone.is_naive(parsed):
        parsed = parsed.replace(tzinfo=datetime_timezone.utc)
    return parsed.astimezone(SAO_PAULO).strftime("%d/%m/%Y às %H:%M")


def _label_from_map(value: Any, labels: dict[str, str]) -> str:
    """
    Return a Portuguese label for a known code.

    Args:
        value: Code stored in the audit record.
        labels: Mapping of code to label.

    Returns:
        str: Human-readable label or the original code.
    """
    code = _text(value).upper()
    return labels.get(code, _text(value))


@dataclass
class _AuditIdSets:
    """
    Identifier sets collected from a batch of audit entries.

    Attributes:
        users: User identifiers referenced by the batch.
        courses: Course identifiers referenced by the batch.
        departments: Department identifiers referenced by the batch.
        invitations: External invitation identifiers referenced by the batch.
        theses: Thesis identifiers referenced by the batch.
        files: Thesis file identifiers referenced by the batch.
        documents: Thesis document identifiers referenced by the batch.
        defenses: Defense identifiers referenced by the batch.
        members: Committee member identifiers referenced by the batch.
    """
    users: set[str]
    courses: set[str]
    departments: set[str]
    invitations: set[str]
    theses: set[str]
    files: set[str]
    documents: set[str]
    defenses: set[str]
    members: set[str]


class AuditPresenter:
    """
    Resolve audit records into readable summaries, entities and detail rows.

    Instances are primed with a batch of records so the administrative listing
    keeps a bounded number of queries regardless of the page size.
    """

    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._courses: dict[str, Any] = {}
        self._departments: dict[str, Any] = {}
        self._invitations: dict[str, ExternalInvitation] = {}
        self._theses: dict[str, Any] = {}
        self._thesis_files: dict[str, Any] = {}
        self._thesis_documents: dict[str, Any] = {}
        self._defenses: dict[str, Any] = {}
        self._members: dict[str, Any] = {}
        self._evaluations: dict[str, Any] = {}
        self._primed = False

    def prime(self, logs: Iterable[AuditLog]) -> None:
        """
        Load related records used by a batch of audit entries.

        Args:
            logs: Audit entries that will be presented.
        """
        if self._primed:
            return
        records = list(logs)
        ids = _AuditIdSets(
            users=set(),
            courses=set(),
            departments=set(),
            invitations=set(),
            theses=set(),
            files=set(),
            documents=set(),
            defenses=set(),
            members=set(),
        )
        for log in records:
            self._collect_log_ids(log, ids)

        self._load_thesis_files(ids.files)
        self._load_thesis_documents(ids.documents)
        self._load_defenses(ids.defenses)
        for thesis_file in self._thesis_files.values():
            ids.theses.add(str(thesis_file.thesis_id))
        for document in self._thesis_documents.values():
            ids.theses.add(str(document.thesis_id))
        for defense in self._defenses.values():
            ids.theses.add(str(defense.thesis_id))
        self._load_theses(ids.theses)
        self._load_users(ids.users)
        self._load_courses(ids.courses)
        self._load_departments(ids.departments)
        self._load_invitations(ids.invitations)
        self._load_members(ids.members)
        self._load_evaluations(ids.members)
        self._primed = True

    def _collect_log_ids(self, log: AuditLog, ids: _AuditIdSets) -> None:
        """
        Collect identifiers referenced by one audit entry.

        Args:
            log: Audit entry being inspected.
            ids: Destination identifier sets.
        """
        metadata = log.metadata if isinstance(log.metadata, dict) else {}
        changes = log.changes if isinstance(log.changes, dict) else {}
        if log.actor_user_id:
            ids.users.add(str(log.actor_user_id))
        if log.actor_external_invitation_id:
            ids.invitations.add(str(log.actor_external_invitation_id))

        entity_id = _uuid(log.entity_id)
        if entity_id:
            if log.entity_type in {EntityType.USER, EntityType.USER_COURSE_ASSIGNMENT}:
                ids.users.add(entity_id)
            elif log.entity_type == EntityType.COURSE:
                ids.courses.add(entity_id)
            elif log.entity_type == EntityType.DEPARTMENT:
                ids.departments.add(entity_id)
            elif log.entity_type == EntityType.EXTERNAL_INVITATION:
                ids.invitations.add(entity_id)
            elif log.entity_type == EntityType.THESIS:
                ids.theses.add(entity_id)
            elif log.entity_type == EntityType.THESIS_FILE:
                ids.files.add(entity_id)
            elif log.entity_type == EntityType.THESIS_DOCUMENT:
                ids.documents.add(entity_id)
            elif log.entity_type == EntityType.DEFENSE:
                ids.defenses.add(entity_id)

        for value in (metadata.get("thesisId"), changes.get("thesisId")):
            identifier = _uuid(value)
            if identifier:
                ids.theses.add(identifier)
        for value in (metadata.get("courseId"), changes.get("courseId")):
            identifier = _uuid(value)
            if identifier:
                ids.courses.add(identifier)
        for value in (metadata.get("departmentId"), changes.get("departmentId")):
            identifier = _uuid(value)
            if identifier:
                ids.departments.add(identifier)
        for value in (metadata.get("advisorId"), metadata.get("coordinatorUserId"), metadata.get("userId")):
            identifier = _uuid(value)
            if identifier:
                ids.users.add(identifier)
        for value in (metadata.get("invitationId"), metadata.get("previousInvitationId")):
            identifier = _uuid(value)
            if identifier:
                ids.invitations.add(identifier)
        member_id = _uuid(metadata.get("memberId"))
        if member_id:
            ids.members.add(member_id)
        document_id = _uuid(metadata.get("documentId"))
        if document_id:
            ids.documents.add(document_id)

    def _load_users(self, user_ids: set[str]) -> None:
        """
        Load users and their active roles in one query.

        Args:
            user_ids: User identifiers referenced by the batch.
        """
        if not user_ids:
            return
        active_roles = UserProfileRole.objects.filter(revoked_at__isnull=True)
        role_prefetch = Prefetch("profile_roles", queryset=active_roles, to_attr="audit_active_roles")
        users = User.objects.filter(pk__in=user_ids).only("id", "name", "email").prefetch_related(role_prefetch)
        for user in users:
            self._users[str(user.pk)] = user

    def _load_courses(self, course_ids: set[str]) -> None:
        """
        Load courses referenced by the batch.

        Args:
            course_ids: Course identifiers referenced by the batch.
        """
        if not course_ids:
            return
        from academic.models import Course

        courses = Course.objects.filter(pk__in=course_ids).select_related("department", "coordinator")
        for course in courses:
            self._courses[str(course.pk)] = course

    def _load_departments(self, department_ids: set[str]) -> None:
        """
        Load departments referenced by the batch.

        Args:
            department_ids: Department identifiers referenced by the batch.
        """
        if not department_ids:
            return
        from academic.models import Department

        for department in Department.objects.filter(pk__in=department_ids):
            self._departments[str(department.pk)] = department

    def _load_invitations(self, invitation_ids: set[str]) -> None:
        """
        Load external invitations referenced by the batch.

        Args:
            invitation_ids: Invitation identifiers referenced by the batch.
        """
        if not invitation_ids:
            return
        for invitation in ExternalInvitation.objects.filter(pk__in=invitation_ids):
            self._invitations[str(invitation.pk)] = invitation

    def _load_theses(self, thesis_ids: set[str]) -> None:
        """
        Load theses with authors, advisor and course.

        Args:
            thesis_ids: Thesis identifiers referenced by the batch.
        """
        if not thesis_ids:
            return
        from theses.models import Thesis, ThesisAuthor

        author_queryset = ThesisAuthor.objects.select_related("author").order_by("position")
        author_prefetch = Prefetch("authors", queryset=author_queryset, to_attr="audit_authors")
        theses = Thesis.objects.filter(pk__in=thesis_ids).select_related("advisor", "course").prefetch_related(author_prefetch)
        for thesis in theses:
            self._theses[str(thesis.pk)] = thesis

    def _load_thesis_files(self, file_ids: set[str]) -> None:
        """
        Load thesis file versions referenced by the batch.

        Args:
            file_ids: Thesis file identifiers referenced by the batch.
        """
        if not file_ids:
            return
        from theses.models import ThesisFile

        for thesis_file in ThesisFile.objects.filter(pk__in=file_ids).select_related("stored_file"):
            self._thesis_files[str(thesis_file.pk)] = thesis_file

    def _load_thesis_documents(self, document_ids: set[str]) -> None:
        """
        Load supplementary thesis documents referenced by the batch.

        Args:
            document_ids: Document identifiers referenced by the batch.
        """
        if not document_ids:
            return
        from theses.models import ThesisDocument

        for document in ThesisDocument.objects.filter(pk__in=document_ids).select_related("stored_file"):
            self._thesis_documents[str(document.pk)] = document

    def _load_defenses(self, defense_ids: set[str]) -> None:
        """
        Load defenses referenced by the batch.

        Args:
            defense_ids: Defense identifiers referenced by the batch.
        """
        if not defense_ids:
            return
        from theses.models import Defense

        for defense in Defense.objects.filter(pk__in=defense_ids):
            self._defenses[str(defense.pk)] = defense

    def _load_members(self, member_ids: set[str]) -> None:
        """
        Load committee members referenced by the batch.

        Args:
            member_ids: Committee member identifiers referenced by the batch.
        """
        if not member_ids:
            return
        from theses.models import DefenseMember

        members = DefenseMember.objects.filter(pk__in=member_ids).select_related("institutional_member", "external_invitation")
        for member in members:
            self._members[str(member.pk)] = member

    def _load_evaluations(self, member_ids: set[str]) -> None:
        """
        Load evaluations so the score can be shown for old audit entries.

        Args:
            member_ids: Committee member identifiers referenced by the batch.
        """
        if not member_ids:
            return
        from theses.models import DefenseMemberEvaluation

        for evaluation in DefenseMemberEvaluation.objects.filter(member_id__in=member_ids):
            self._evaluations[str(evaluation.member_id)] = evaluation

    def _metadata(self, log: AuditLog) -> dict[str, Any]:
        """
        Return the metadata mapping of an audit entry.

        Args:
            log: Audit entry.

        Returns:
            dict[str, Any]: Metadata mapping or an empty mapping.
        """
        return log.metadata if isinstance(log.metadata, dict) else {}

    def _changes(self, log: AuditLog) -> dict[str, Any]:
        """
        Return the changes mapping of an audit entry.

        Args:
            log: Audit entry.

        Returns:
            dict[str, Any]: Changes mapping or an empty mapping.
        """
        return log.changes if isinstance(log.changes, dict) else {}

    def _event(self, log: AuditLog) -> str:
        """
        Return the internal event code stored in metadata.

        Args:
            log: Audit entry.

        Returns:
            str: Event code or an empty string.
        """
        return _text(self._metadata(log).get("event"))

    def _user(self, value: Any) -> User | None:
        """
        Resolve a user identifier from the batch cache.

        Args:
            value: Raw user identifier.

        Returns:
            User | None: Cached user when available.
        """
        identifier = _uuid(value)
        if identifier is None:
            return None
        return self._users.get(identifier)

    def _course(self, value: Any) -> Any | None:
        """
        Resolve a course identifier from the batch cache.

        Args:
            value: Raw course identifier.

        Returns:
            Any | None: Cached course when available.
        """
        identifier = _uuid(value)
        if identifier is None:
            return None
        return self._courses.get(identifier)

    def _invitation(self, value: Any) -> ExternalInvitation | None:
        """
        Resolve an invitation identifier from the batch cache.

        Args:
            value: Raw invitation identifier.

        Returns:
            ExternalInvitation | None: Cached invitation when available.
        """
        identifier = _uuid(value)
        if identifier is None:
            return None
        return self._invitations.get(identifier)

    def _thesis(self, value: Any) -> Any | None:
        """
        Resolve a thesis identifier from the batch cache.

        Args:
            value: Raw thesis identifier.

        Returns:
            Any | None: Cached thesis when available.
        """
        identifier = _uuid(value)
        if identifier is None:
            return None
        return self._theses.get(identifier)

    def _thesis_for_log(self, log: AuditLog) -> Any | None:
        """
        Resolve the thesis associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            Any | None: Thesis when it can be resolved.
        """
        entity_id = _uuid(log.entity_id)
        if log.entity_type == EntityType.THESIS and entity_id:
            return self._theses.get(entity_id)
        if log.entity_type == EntityType.THESIS_FILE and entity_id:
            thesis_file = self._thesis_files.get(entity_id)
            if thesis_file is not None:
                return self._theses.get(str(thesis_file.thesis_id))
        if log.entity_type == EntityType.THESIS_DOCUMENT and entity_id:
            document = self._thesis_documents.get(entity_id)
            if document is not None:
                return self._theses.get(str(document.thesis_id))
        if log.entity_type == EntityType.DEFENSE and entity_id:
            defense = self._defenses.get(entity_id)
            if defense is not None:
                return self._theses.get(str(defense.thesis_id))
        return self._thesis(self._metadata(log).get("thesisId"))

    def _defense_for_log(self, log: AuditLog) -> Any | None:
        """
        Resolve the defense associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            Any | None: Defense when it can be resolved.
        """
        entity_id = _uuid(log.entity_id)
        if log.entity_type == EntityType.DEFENSE and entity_id:
            return self._defenses.get(entity_id)
        return None

    def _member_for_log(self, log: AuditLog) -> Any | None:
        """
        Resolve the committee member associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            Any | None: Committee member when it can be resolved.
        """
        member_id = _uuid(self._metadata(log).get("memberId"))
        if member_id is None:
            return None
        return self._members.get(member_id)

    def _evaluation_for_log(self, log: AuditLog) -> Any | None:
        """
        Resolve the evaluation associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            Any | None: Evaluation when it can be resolved.
        """
        member_id = _uuid(self._metadata(log).get("memberId"))
        if member_id is None:
            return None
        return self._evaluations.get(member_id)

    def _author_names(self, log: AuditLog) -> list[str]:
        """
        Return author names for the thesis associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            list[str]: Author names in display order.
        """
        thesis = self._thesis_for_log(log)
        if thesis is not None:
            authors = getattr(thesis, "audit_authors", None)
            if authors is None:
                authors = list(thesis.authors.select_related("author").order_by("position"))
            names = []
            for link in authors:
                name = _text(getattr(link.author, "name", "")) or _text(getattr(link.author, "email", ""))
                if name:
                    names.append(name)
            if names:
                return names
        metadata = self._metadata(log)
        raw_names = metadata.get("thesisAuthorNames") or metadata.get("authorNames")
        if isinstance(raw_names, (list, tuple)):
            return [_text(item) for item in raw_names if _text(item)]
        author_name = _text(metadata.get("authorName"))
        return [author_name] if author_name else []

    def _author_phrase(self, log: AuditLog) -> str:
        """
        Return a Portuguese phrase describing the thesis authors.

        Args:
            log: Audit entry.

        Returns:
            str: ``do autor X``, ``dos autores X et al.`` or an empty string.
        """
        names = self._author_names(log)
        if not names:
            return ""
        if len(names) == 1:
            return f"do autor {names[0]}"
        return f"dos autores {names[0]} et al."

    def _thesis_title(self, log: AuditLog) -> str:
        """
        Return the thesis title associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            str: Thesis title or an empty string.
        """
        thesis = self._thesis_for_log(log)
        if thesis is not None:
            title = _text(getattr(thesis, "title", ""))
            if title:
                return title
        metadata = self._metadata(log)
        return _text(metadata.get("thesisTitle")) or _text(metadata.get("title"))

    def _thesis_title_short(self, log: AuditLog) -> str:
        """
        Return a truncated thesis title for the audit summary.

        Args:
            log: Audit entry.

        Returns:
            str: Title shortened at a word boundary with an ellipsis.
        """
        return _truncate_text(self._thesis_title(log))

    def _thesis_reference(self, log: AuditLog) -> str:
        """
        Return the thesis reference code associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            str: Reference code or an empty string.
        """
        thesis = self._thesis_for_log(log)
        if thesis is not None:
            reference = _text(getattr(thesis, "reference_code", ""))
            if reference:
                return reference
        metadata = self._metadata(log)
        return _text(metadata.get("thesisReference")) or _text(metadata.get("referenceCode"))

    def _advisor_name(self, log: AuditLog) -> str:
        """
        Return the advisor name associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            str: Advisor name or an empty string.
        """
        thesis = self._thesis_for_log(log)
        if thesis is not None and getattr(thesis, "advisor", None) is not None:
            name = _text(getattr(thesis.advisor, "name", "")) or _text(getattr(thesis.advisor, "email", ""))
            if name:
                return name
        metadata = self._metadata(log)
        advisor_name = _text(metadata.get("advisorName"))
        if advisor_name:
            return advisor_name
        advisor = self._user(metadata.get("advisorId"))
        if advisor is not None:
            return _text(advisor.name) or _text(advisor.email)
        return ""

    def _course_name(self, log: AuditLog) -> str:
        """
        Return the course name associated with an audit entry.

        Args:
            log: Audit entry.

        Returns:
            str: Course name or an empty string.
        """
        thesis = self._thesis_for_log(log)
        if thesis is not None and getattr(thesis, "course", None) is not None:
            name = _text(getattr(thesis.course, "name", ""))
            if name:
                return name
        metadata = self._metadata(log)
        course_name = _text(metadata.get("courseName"))
        if course_name:
            return course_name
        changes = self._changes(log)
        course_name = _text(changes.get("course"))
        if course_name:
            return course_name
        course = self._course(metadata.get("courseId") or changes.get("courseId"))
        if course is not None:
            return _text(course.name)
        return ""

    def actor_name(self, log: AuditLog) -> str:
        """
        Return the actor display name.

        Args:
            log: Audit entry.

        Returns:
            str: User name, external invitee name or ``Sistema``.
        """
        if log.actor_user_id:
            user = self._user(log.actor_user_id) or log.actor_user
            if user is not None:
                return _text(user.name) or _text(user.email)
        if log.actor_external_invitation_id:
            invitation = self._invitation(log.actor_external_invitation_id) or log.actor_external_invitation
            if invitation is not None:
                return _text(invitation.name) or _text(invitation.email)
        return "Sistema"

    def actor_email(self, log: AuditLog) -> str | None:
        """
        Return the actor e-mail when it exists.

        Args:
            log: Audit entry.

        Returns:
            str | None: Actor e-mail or ``None``.
        """
        if log.actor_user_id:
            user = self._user(log.actor_user_id) or log.actor_user
            if user is not None:
                return _text(user.email) or None
        if log.actor_external_invitation_id:
            invitation = self._invitation(log.actor_external_invitation_id) or log.actor_external_invitation
            if invitation is not None:
                return _text(invitation.email) or None
        return None

    def role_codes(self, log: AuditLog) -> list[str]:
        """
        Return every role held by the actor, in business priority order.

        Args:
            log: Audit entry.

        Returns:
            list[str]: Role codes; ``EXTERNAL`` for token sessions and ``SYSTEM``
                when there is no institutional actor.
        """
        stored_roles = log.actor_roles if isinstance(log.actor_roles, list) else []
        valid_roles = {role.value for role in ProfileRole}
        codes = [str(role) for role in stored_roles if str(role) in valid_roles]
        if not codes and log.actor_role in valid_roles:
            codes = [str(log.actor_role)]
        if not codes and log.actor_user_id:
            user = self._user(log.actor_user_id) or log.actor_user
            if user is not None:
                active_roles = getattr(user, "audit_active_roles", None)
                if active_roles is None:
                    active_roles = user.active_profile_roles().values_list("role", flat=True)
                assigned = {str(role) for role in active_roles}
                if assigned:
                    codes = [role.value for role in ROLE_PRIORITY if role.value in assigned]
                else:
                    return []
        if not codes and log.actor_external_invitation_id:
            return ["EXTERNAL"]
        if not codes:
            return ["SYSTEM"]
        ordered = [role.value for role in ROLE_PRIORITY if role.value in set(codes)]
        return ordered or codes

    def roles(self, log: AuditLog) -> list[dict[str, str]]:
        """
        Return actor roles with their labels.

        Args:
            log: Audit entry.

        Returns:
            list[dict[str, str]]: Role code and label pairs.
        """
        result = []
        for code in self.role_codes(log):
            if code == "EXTERNAL":
                result.append({"code": code, "label": "Membro externo"})
            elif code == "SYSTEM":
                result.append({"code": code, "label": "Sistema"})
            else:
                result.append({"code": code, "label": role_label(code) or code})
        return result

    def role_text(self, log: AuditLog) -> str:
        """
        Return a single string with every actor role label.

        Args:
            log: Audit entry.

        Returns:
            str: Comma-separated role labels.
        """
        labels = [item["label"] for item in self.roles(log)]
        return ", ".join(labels) if labels else "Sem perfil"

    def category(self, log: AuditLog) -> str:
        """
        Return the category of an audit entry.

        Args:
            log: Audit entry.

        Returns:
            AuditCategory: The derived verbosity category.
        """
        stored = getattr(log, "category", "")
        if stored:
            return stored
        metadata = self._metadata(log)
        return derive_audit_category(log.action, log.entity_type, metadata)

    def category_label(self, log: AuditLog) -> str:
        """
        Return the Portuguese label of the entry category.

        Args:
            log: Audit entry.

        Returns:
            str: Category label.
        """
        return CATEGORY_LABELS.get(self.category(log), self.category(log))

    def verbosity(self, log: AuditLog) -> str:
        """
        Backward-compatible coarse classification.

        Args:
            log: Audit entry.

        Returns:
            str: ``TRACE`` for authentication/access records, ``RELEVANT`` otherwise.
        """
        return "TRACE" if self.category(log) in {CATEGORY_ACCESS} else "RELEVANT"

    def verbosity_label(self, log: AuditLog) -> str:
        """
        Backward-compatible coarse label.

        Args:
            log: Audit entry.

        Returns:
            str: Coarse verbosity label.
        """
        return "Rastreamento" if self.verbosity(log) == "TRACE" else "Operação relevante"

    def entity_type_label(self, log: AuditLog) -> str:
        """
        Return a human-readable entity type.

        Args:
            log: Audit entry.

        Returns:
            str: Entity type label.
        """
        return ENTITY_TYPE_LABELS.get(log.entity_type, log.entity_type)

    def entity_name(self, log: AuditLog) -> str:
        """
        Return the human name of the entity affected by the action.

        Args:
            log: Audit entry.

        Returns:
            str: User name, course name, thesis title or another readable name.
        """
        metadata = self._metadata(log)
        changes = self._changes(log)
        entity_id = _uuid(log.entity_id)
        if log.entity_type == EntityType.USER and entity_id:
            user = self._users.get(entity_id)
            if user is not None:
                return _text(user.name) or _text(user.email)
            return _text(metadata.get("name")) or _text(metadata.get("email"))
        if log.entity_type == EntityType.USER_COURSE_ASSIGNMENT:
            user = self._users.get(entity_id) if entity_id else None
            if user is not None:
                return _text(user.name) or _text(user.email)
            return _text(metadata.get("userName")) or _text(metadata.get("userEmail"))
        if log.entity_type == EntityType.COURSE:
            course = self._courses.get(entity_id) if entity_id else None
            if course is not None:
                return _text(course.name)
            return _text(changes.get("name")) or _text(metadata.get("name"))
        if log.entity_type == EntityType.DEPARTMENT:
            department = self._departments.get(entity_id) if entity_id else None
            if department is not None:
                return _text(department.name)
            return _text(changes.get("name")) or _text(metadata.get("name"))
        if log.entity_type == EntityType.EXTERNAL_INVITATION:
            invitation = self._invitations.get(entity_id) if entity_id else None
            if invitation is not None:
                return _text(invitation.name)
            return _text(metadata.get("name")) or _text(metadata.get("email"))
        if log.entity_type in {EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT, EntityType.DEFENSE}:
            return self._thesis_title(log)
        if log.entity_type == EntityType.REPORT_OVERVIEW:
            return "Relatório geral"
        if log.entity_type == EntityType.SESSION_AUDIT:
            user = self._user(metadata.get("userId"))
            return _text(user.name) if user is not None else "Sessão"
        if log.entity_type == EntityType.AUDIT_LOG:
            return "Registro de auditoria"
        return _text(metadata.get("name")) or _text(metadata.get("title")) or _text(log.entity_id)

    def entity_name_short(self, log: AuditLog) -> str:
        """
        Return a truncated entity name for table cells.

        Args:
            log: Audit entry.

        Returns:
            str: Name shortened at a word boundary with an ellipsis when it
                exceeds the summary limit.
        """
        return _truncate_text(self.entity_name(log))

    def entity_reference(self, log: AuditLog) -> str:
        """
        Return a short reference for the entity, suitable for a table cell.

        Args:
            log: Audit entry.

        Returns:
            str: Course code, user e-mail, thesis reference code or raw identifier.
        """
        metadata = self._metadata(log)
        changes = self._changes(log)
        entity_id = _uuid(log.entity_id)
        if log.entity_type == EntityType.USER and entity_id:
            user = self._users.get(entity_id)
            if user is not None:
                return _text(user.email)
            return _text(metadata.get("email")) or _text(log.entity_id)
        if log.entity_type == EntityType.USER_COURSE_ASSIGNMENT and entity_id:
            user = self._users.get(entity_id)
            if user is not None:
                return _text(user.email)
            return _text(metadata.get("userEmail")) or _text(log.entity_id)
        if log.entity_type == EntityType.COURSE:
            course = self._courses.get(entity_id) if entity_id else None
            if course is not None:
                return _text(course.code) or _text(course.name)
            return _text(changes.get("code")) or _text(metadata.get("code")) or _text(log.entity_id)
        if log.entity_type == EntityType.DEPARTMENT:
            department = self._departments.get(entity_id) if entity_id else None
            if department is not None:
                return _text(department.name)
            return _text(changes.get("name")) or _text(log.entity_id)
        if log.entity_type == EntityType.EXTERNAL_INVITATION:
            invitation = self._invitations.get(entity_id) if entity_id else None
            if invitation is not None:
                return _text(invitation.email)
            return _text(metadata.get("email")) or _text(log.entity_id)
        if log.entity_type == EntityType.THESIS:
            return self._thesis_reference(log) or _text(log.entity_id)
        if log.entity_type == EntityType.THESIS_FILE:
            reference = self._thesis_reference(log)
            thesis_file = self._thesis_files.get(entity_id) if entity_id else None
            version = _text(metadata.get("versionNumber"))
            if thesis_file is not None:
                version = _text(thesis_file.version_number)
            if reference and version:
                return f"{reference} · versão {version}"
            return reference or version or _text(log.entity_id)
        if log.entity_type == EntityType.THESIS_DOCUMENT:
            reference = self._thesis_reference(log)
            document = self._thesis_documents.get(entity_id) if entity_id else None
            document_type = _text(metadata.get("documentType"))
            if document is not None:
                document_type = _text(document.type)
            type_label = _label_from_map(document_type, DOCUMENT_TYPE_LABELS)
            if reference and type_label:
                return f"{reference} · {type_label}"
            return reference or type_label or _text(log.entity_id)
        if log.entity_type == EntityType.DEFENSE:
            return self._thesis_reference(log) or _text(log.entity_id)
        if log.entity_type == EntityType.REPORT_OVERVIEW:
            return "Visão geral"
        return _text(log.entity_id)

    def entity_phrase(self, log: AuditLog) -> str:
        """
        Return a Portuguese phrase naming the affected entity.

        Args:
            log: Audit entry.

        Returns:
            str: Phrase such as ``o curso "Engenharia"`` or ``o TCC "Título"``.
        """
        name = self.entity_name(log)
        title = self._thesis_title_short(log)
        if log.entity_type == EntityType.USER:
            base = name or "usuário não identificado"
            return f"o usuário {base}"
        if log.entity_type == EntityType.COURSE:
            return f"o curso \"{name}\"" if name else "o curso"
        if log.entity_type == EntityType.DEPARTMENT:
            return f"o departamento \"{name}\"" if name else "o departamento"
        if log.entity_type == EntityType.USER_COURSE_ASSIGNMENT:
            return f"o vínculo do usuário {name}" if name else "o vínculo de curso"
        if log.entity_type == EntityType.EXTERNAL_INVITATION:
            return f"o convite externo de {name}" if name else "o convite externo"
        if log.entity_type in {EntityType.THESIS, EntityType.THESIS_FILE}:
            return f"o TCC \"{title}\"" if title else "o TCC"
        if log.entity_type == EntityType.THESIS_DOCUMENT:
            document = self._thesis_documents.get(_uuid(log.entity_id) or "")
            document_type = _text(self._metadata(log).get("documentType"))
            if document is not None:
                document_type = _text(document.type)
            type_label = _label_from_map(document_type, DOCUMENT_TYPE_LABELS)
            if title and type_label:
                return f"o documento {type_label} do TCC \"{title}\""
            if title:
                return f"o documento do TCC \"{title}\""
            return "o documento do TCC"
        if log.entity_type == EntityType.DEFENSE:
            return f"a defesa do TCC \"{title}\"" if title else "a defesa"
        if log.entity_type == EntityType.REPORT_OVERVIEW:
            return "o relatório geral"
        if log.entity_type == EntityType.SESSION_AUDIT:
            return "a sessão"
        if log.entity_type == EntityType.AUDIT_LOG:
            return "o registro de auditoria"
        return f"o registro de {self.entity_type_label(log).lower()}"

    def _member_text(self, log: AuditLog) -> str:
        """
        Return the committee member name and role.

        Args:
            log: Audit entry.

        Returns:
            str: ``Nome (Função)`` or an empty string.
        """
        metadata = self._metadata(log)
        member = self._member_for_log(log)
        if member is not None:
            name = _text(member.name) or _text(member.email)
            role = _label_from_map(member.role, COMMITTEE_ROLE_LABELS)
            return f"{name} ({role})" if role else name
        name = _text(metadata.get("memberName"))
        role = _label_from_map(metadata.get("memberRole"), COMMITTEE_ROLE_LABELS)
        if name and role:
            return f"{name} ({role})"
        return name

    def _decision_label(self, value: Any) -> str:
        """
        Return the Portuguese label of a defense or review decision.

        Args:
            value: Decision code.

        Returns:
            str: Decision label or the original value.
        """
        code = _text(value).upper()
        if code in DEFENSE_RESULT_LABELS:
            return DEFENSE_RESULT_LABELS[code]
        return REVIEW_DECISION_LABELS.get(code, _text(value))

    def _result_label(self, value: Any) -> str:
        """
        Return the Portuguese label of a defense result.

        Args:
            value: Result code.

        Returns:
            str: Result label or the original value.
        """
        return _label_from_map(value, DEFENSE_RESULT_LABELS)

    def _document_type_label(self, value: Any) -> str:
        """
        Return the Portuguese label of a document type.

        Args:
            value: Document type code.

        Returns:
            str: Document type label or the original value.
        """
        return _label_from_map(value, DOCUMENT_TYPE_LABELS)

    def _user_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a user action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        changes = self._changes(log)
        name = self.entity_name(log) or "usuário não identificado"
        email = _text(metadata.get("email"))
        identity = f"{name} ({email})" if email and email != name else name
        if log.action == AuditAction.CREATE:
            roles = changes.get("roles") or metadata.get("roles")
            role_labels = [role_label(str(item)) or str(item) for item in roles] if isinstance(roles, (list, tuple)) else []
            suffix = f" com o perfil {', '.join(role_labels)}" if role_labels else ""
            return f"Criou o usuário {identity}{suffix}"
        if log.action == AuditAction.DELETE:
            return f"Desativou o usuário {identity}"
        if log.action == AuditAction.UPDATE:
            role_change = changes.get("roles")
            if isinstance(role_change, dict):
                before = [role_label(str(item)) or str(item) for item in role_change.get("from") or []]
                after = [role_label(str(item)) or str(item) for item in role_change.get("to") or []]
                before_text = ", ".join(before) or "nenhum"
                after_text = ", ".join(after) or "nenhum"
                noun = "perfil" if len(after) <= 1 and len(before) <= 1 else "perfis"
                return f"Atualizou os {noun} do usuário {identity} de {before_text} para {after_text}"
            status_change = changes.get("is_active")
            if isinstance(status_change, dict) and status_change.get("to") is True:
                return f"Reativou o usuário {identity}"
            course_change = changes.get("course")
            if isinstance(course_change, dict):
                before = _text(course_change.get("from")) or "sem curso"
                after = _text(course_change.get("to")) or "sem curso"
                return f"Alterou o curso do usuário {identity} de {before} para {after}"
            return f"Atualizou o usuário {identity}"
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} o usuário {identity}"

    def _course_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a course action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        changes = self._changes(log)
        name = self.entity_name(log) or "curso não identificado"
        code = _text(changes.get("code")) or _text(metadata.get("code"))
        suffix = f" ({code})" if code else ""
        if log.action == AuditAction.CREATE:
            return f"Criou o curso \"{name}\"{suffix}"
        if log.action == AuditAction.DELETE:
            return f"Excluiu o curso \"{name}\"{suffix}"
        if log.action == AuditAction.UPDATE:
            coordinator_id = _text(changes.get("coordinatorUserId"))
            if changes.get("coordinatorResolved") and coordinator_id:
                coordinator = self._user(coordinator_id)
                coordinator_name = _text(coordinator.name) if coordinator is not None else _text(changes.get("coordinatorEmail"))
                if coordinator_name:
                    return f"Definiu {coordinator_name} como coordenador do curso \"{name}\""
            return f"Atualizou o curso \"{name}\"{suffix}"
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} o curso \"{name}\""

    def _department_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a department action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        name = self.entity_name(log) or "departamento não identificado"
        if log.action == AuditAction.CREATE:
            return f"Criou o departamento \"{name}\""
        if log.action == AuditAction.DELETE:
            return f"Excluiu o departamento \"{name}\""
        if log.action == AuditAction.UPDATE:
            return f"Atualizou o departamento \"{name}\""
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} o departamento \"{name}\""

    def _assignment_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a user-course assignment action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        changes = self._changes(log)
        name = self.entity_name(log) or "usuário não identificado"
        course_value = changes.get("course")
        course_id = changes.get("courseId")
        if course_value in (None, "", False) and ("course" in changes or "courseId" in changes):
            return f"Removeu o vínculo do usuário {name} com o curso"
        course = self._course(course_id)
        course_name = _text(course_value) or (_text(course.name) if course is not None else "")
        if not course_name:
            course_name = _text(metadata.get("courseName"))
        if course_name:
            return f"Vinculou o usuário {name} ao curso \"{course_name}\""
        return f"Atualizou o vínculo do usuário {name} com o curso"

    def _invitation_summary(self, log: AuditLog) -> str:
        """
        Build the summary of an external invitation action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        name = self.entity_name(log) or "convidado não identificado"
        email = _text(metadata.get("email"))
        identity = f"{name} ({email})" if email and email != name else name
        if log.action == AuditAction.INVITE:
            return f"Convidou {identity} para participar de uma banca"
        if log.action == AuditAction.TOKEN_ACCESS:
            return f"Acessou o sistema com o convite externo de {identity}"
        if log.action == AuditAction.DELETE:
            return f"Revogou o convite externo de {identity}"
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} o convite externo de {identity}"

    def _thesis_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a thesis, file or document action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        event = self._event(log)
        title = self._thesis_title_short(log)
        title_with_quotes = f"\"{title}\"" if title else ""
        thesis_phrase = f"o TCC {title_with_quotes}" if title else "o TCC"
        thesis_of_phrase = f"do TCC {title_with_quotes}" if title else "do TCC"
        if log.entity_type == EntityType.THESIS_FILE:
            version = _text(metadata.get("versionNumber"))
            thesis_file = self._thesis_files.get(_uuid(log.entity_id) or "")
            if thesis_file is not None:
                version = _text(thesis_file.version_number)
            disposition = _text(metadata.get("disposition"))
            verb = "Visualizou" if disposition == "inline" else "Baixou"
            if version:
                return f"{verb} a versão {version} {thesis_of_phrase}"
            return f"{verb} {thesis_phrase}"
        if log.entity_type == EntityType.THESIS_DOCUMENT:
            document = self._thesis_documents.get(_uuid(log.entity_id) or "")
            document_type = _text(metadata.get("documentType"))
            if document is not None:
                document_type = _text(document.type)
            type_label = self._document_type_label(document_type)
            document_phrase = f"o documento {type_label} {thesis_of_phrase}" if type_label else f"o documento {thesis_of_phrase}"
            if log.action == AuditAction.DOWNLOAD:
                return f"Baixou {document_phrase}"
            if log.action == AuditAction.UPLOAD:
                return f"Enviou {document_phrase}"
            if log.action == AuditAction.APPROVE:
                return f"Validou {document_phrase}"
            return f"{ACTION_VERBS.get(log.action, log.get_action_display())} {document_phrase}"
        if event == AuditEvent.SUBMITTED:
            advisor = self._advisor_name(log)
            suffix = f" para o orientador {advisor}" if advisor else ""
            return f"Submeteu {thesis_phrase}{suffix}"
        if event == AuditEvent.SUBMISSION_WITHDRAWN:
            return f"Retirou a submissão {thesis_of_phrase}"
        if event == AuditEvent.VERSION_HISTORY_PURGED:
            removed = _text(metadata.get("removedVersions"))
            suffix = f" ({removed} versões antigas)" if removed else ""
            return f"Removeu versões antigas {thesis_of_phrase}{suffix}"
        if event == AuditEvent.DEFENSE_RESULT:
            result = self._result_label(metadata.get("result"))
            suffix = f": {result}" if result else ""
            return f"Registrou o resultado da defesa {thesis_of_phrase}{suffix}"
        if event == AuditEvent.DOCUMENT_VALIDATED:
            type_label = self._document_type_label(metadata.get("documentType"))
            document_phrase = f"o documento {type_label} {thesis_of_phrase}" if type_label else f"o documento {thesis_of_phrase}"
            return f"Validou {document_phrase}"
        if event == AuditEvent.PUBLISHED:
            return f"Publicou {thesis_phrase} no repositório"
        if log.action == AuditAction.CREATE:
            return f"Criou {thesis_phrase}"
        if log.action == AuditAction.UPLOAD:
            document_type = _text(metadata.get("documentType"))
            if document_type:
                type_label = self._document_type_label(document_type)
                document_phrase = f"o documento {type_label} {thesis_of_phrase}" if type_label else f"o documento {thesis_of_phrase}"
                return f"Enviou {document_phrase}"
            version = _text(metadata.get("versionNumber"))
            if version:
                return f"Enviou a versão {version} {thesis_of_phrase}"
            return f"Enviou {thesis_phrase}"
        if log.action == AuditAction.UPDATE:
            decision = _text(metadata.get("decision"))
            if decision == "APPROVED":
                return f"Aprovou {thesis_phrase}"
            if decision == "CHANGES_REQUESTED":
                return f"Solicitou ajustes no TCC {title_with_quotes}" if title else "Solicitou ajustes no TCC"
            if decision == "REJECTED":
                return f"Rejeitou {thesis_phrase}"
            return f"Atualizou os dados {thesis_of_phrase}"
        if log.action == AuditAction.APPROVE:
            return f"Aprovou {thesis_phrase}"
        if log.action == AuditAction.REJECT:
            return f"Rejeitou {thesis_phrase}"
        if log.action == AuditAction.DELETE:
            return f"Excluiu {thesis_phrase}"
        if log.action == AuditAction.PUBLISH:
            return f"Publicou {thesis_phrase} no repositório"
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} {thesis_phrase}".strip()

    def _defense_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a defense action.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        event = self._event(log)
        title = self._thesis_title_short(log)
        thesis_of_phrase = f"do TCC \"{title}\"" if title else "do TCC"
        defense_phrase = f"a defesa {thesis_of_phrase}"
        defense_of_phrase = f"da defesa {thesis_of_phrase}"
        defense_to_phrase = f"à defesa {thesis_of_phrase}"
        defense_for_phrase = f"para a defesa {thesis_of_phrase}"
        committee_phrase = f"a banca {thesis_of_phrase}"
        committee_of_phrase = f"da banca {thesis_of_phrase}"
        if event == AuditEvent.DEFENSE_SCHEDULED:
            scheduled = metadata.get("scheduledDatetime")
            suffix = f" para {_format_datetime(scheduled)}" if scheduled else ""
            return f"Agendou {defense_phrase}{suffix}"
        if event == AuditEvent.AUTO_STARTED_BY_SCHEDULE:
            return f"Iniciou automaticamente {defense_phrase}"
        if event == AuditEvent.DEFENSE_UPDATED:
            return f"Atualizou o agendamento {defense_of_phrase}"
        if event == AuditEvent.DEFENSE_CANCELLED:
            reason = _text(metadata.get("reason"))
            suffix = f". Motivo: {reason}" if reason else ""
            return f"Cancelou {defense_phrase}{suffix}"
        if event == AuditEvent.DEFENSE_STARTED:
            return f"Iniciou {defense_phrase}"
        if event == AuditEvent.DEFENSE_CLOSED:
            result = self._result_label(metadata.get("result"))
            trigger = _text(metadata.get("trigger"))
            automatic = " automaticamente" if trigger == "AUTO_AFTER_LAST_EVALUATION" else ""
            suffix = f" com resultado {result}" if result else ""
            return f"Encerrou{automatic} {defense_phrase}{suffix}"
        if event == AuditEvent.STATUS_REVERTED_BY_ADMIN:
            before = _label_from_map(metadata.get("from"), DEFENSE_STATUS_LABELS)
            after = _label_from_map(metadata.get("to"), DEFENSE_STATUS_LABELS)
            reason = _text(metadata.get("reason"))
            suffix = f". Motivo: {reason}" if reason else ""
            return f"Reverteu o status {defense_of_phrase} de {before} para {after}{suffix}"
        if event == AuditEvent.MEMBER_ADDED:
            member = self._member_text(log)
            if _text(metadata.get("type")) == "external":
                return f"Convidou {member} para {committee_phrase}"
            return f"Adicionou {member} à banca {thesis_of_phrase}"
        if event == AuditEvent.MEMBER_REMOVED:
            member = self._member_text(log)
            return f"Removeu {member} {committee_of_phrase}"
        if event == AuditEvent.AVAILABILITY_RESPONDED:
            member = self._member_text(log)
            answer = _label_from_map(metadata.get("to"), AVAILABILITY_STATUS_LABELS).lower()
            return f"{member} respondeu à disponibilidade {defense_for_phrase}: {answer}"
        if event == AuditEvent.EXTERNAL_TOKEN_REGENERATED:
            member = self._member_text(log)
            return f"Gerou um novo token de acesso para {member}, {committee_of_phrase}"
        if event == AuditEvent.EVALUATION_SUBMITTED:
            evaluation = self._evaluation_for_log(log)
            score = ""
            decision = _text(metadata.get("decision"))
            if evaluation is not None:
                score = _format_score(evaluation.score)
                decision = _text(evaluation.decision)
            if not score:
                score = _format_score(metadata.get("score"))
            author_phrase = self._author_phrase(log)
            parts = [f"Avaliou {defense_phrase}"]
            if author_phrase:
                parts.append(author_phrase)
            if score:
                parts.append(f"com {score} pontos")
            elif decision:
                parts.append(f"com decisão {self._decision_label(decision)}")
            return " ".join(parts)
        if event == AuditEvent.EVALUATION_BLOCKED_AFTER_CLOSE:
            return f"Tentou avaliar {defense_to_phrase} após o encerramento"
        if log.action == AuditAction.SCHEDULE:
            return f"Agendou {defense_phrase}"
        if log.action == AuditAction.UPDATE:
            return f"Atualizou {defense_phrase}"
        if log.action == AuditAction.CREATE:
            return f"Criou {defense_phrase}"
        if log.action == AuditAction.DELETE:
            return f"Removeu informações {defense_of_phrase}"
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} {defense_phrase}".strip()

    def _report_summary(self, log: AuditLog) -> str:
        """
        Build the summary of a report export.

        Args:
            log: Audit entry.

        Returns:
            str: Readable summary.
        """
        metadata = self._metadata(log)
        scope = _text(metadata.get("scope"))
        scope_text = "dos cursos coordenados" if scope == "COORDINATED_COURSES" else "de todos os cursos"
        filters = []
        course = self._course(metadata.get("courseId"))
        if course is not None:
            filters.append(f"curso: {_text(course.name)}")
        advisor = self._user(metadata.get("advisorId"))
        if advisor is not None:
            advisor_name = _text(advisor.name) or _text(advisor.email)
            filters.append(f"orientador: {advisor_name}")
        year = _text(metadata.get("year"))
        if year:
            filters.append(f"ano de defesa: {year}")
        suffix = f" ({'; '.join(filters)})" if filters else ""
        return f"Exportou o relatório geral em PDF, {scope_text}{suffix}"

    def summary(self, log: AuditLog) -> str:
        """
        Build a high-level, human-readable sentence for an audit entry.

        Args:
            log: Audit entry.

        Returns:
            str: Sentence shown in the audit table.
        """
        metadata = self._metadata(log)
        if log.action == AuditAction.LOGIN:
            if log.actor_external_invitation_id:
                invitation = self._invitation(log.actor_external_invitation_id) or log.actor_external_invitation
                name = _text(getattr(invitation, "name", "")) or _text(getattr(invitation, "email", ""))
                return f"Entrou no sistema com o convite externo de {name}" if name else "Entrou no sistema com um convite externo"
            return "Entrou no sistema"
        if log.action == AuditAction.LOGOUT:
            return "Saiu do sistema"
        if log.action == AuditAction.LOGIN_FAILED:
            identifier = _text(metadata.get("email")) or _text(log.entity_id)
            if identifier and "@" in identifier:
                return f"Tentou entrar no sistema sem sucesso com o e-mail {identifier}"
            return "Tentou entrar no sistema sem sucesso"
        if log.entity_type == EntityType.USER:
            return self._user_summary(log)
        if log.entity_type == EntityType.COURSE:
            return self._course_summary(log)
        if log.entity_type == EntityType.DEPARTMENT:
            return self._department_summary(log)
        if log.entity_type == EntityType.USER_COURSE_ASSIGNMENT:
            return self._assignment_summary(log)
        if log.entity_type == EntityType.EXTERNAL_INVITATION:
            return self._invitation_summary(log)
        if log.entity_type in {EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT}:
            return self._thesis_summary(log)
        if log.entity_type == EntityType.DEFENSE:
            return self._defense_summary(log)
        if log.entity_type == EntityType.REPORT_OVERVIEW:
            return self._report_summary(log)
        return f"{ACTION_VERBS.get(log.action, log.get_action_display())} {self.entity_phrase(log)}"

    def _format_value(self, log: AuditLog, key: str, value: Any) -> str | None:
        """
        Format one metadata or change value for the detail panel.

        Args:
            log: Audit entry being presented.
            key: Field name.
            value: Raw value.

        Returns:
            str | None: Readable value, or ``None`` when the value should be hidden.
        """
        if key in SKIP_DETAIL_KEYS:
            return None
        if value is None or value == "":
            return "—"
        if key in {"roles", "role"} and isinstance(value, (list, tuple)):
            return ", ".join(role_label(str(item)) or str(item) for item in value)
        if key == "role":
            return _label_from_map(value, COMMITTEE_ROLE_LABELS)
        if key == "is_active":
            return "Ativo" if bool(value) else "Inativo"
        if key == "decision":
            return self._decision_label(value)
        if key == "result":
            return self._result_label(value)
        if key in {"thesisNewStatus", "targetStatus"}:
            return _label_from_map(value, THESIS_STATUS_LABELS)
        if key == "status":
            code = _text(value).upper()
            if code in INVITATION_STATUS_LABELS:
                return INVITATION_STATUS_LABELS[code]
            if code in DEFENSE_STATUS_LABELS:
                return DEFENSE_STATUS_LABELS[code]
            if code in THESIS_STATUS_LABELS:
                return THESIS_STATUS_LABELS[code]
            return _text(value)
        if key == "documentType":
            return self._document_type_label(value)
        if key == "disposition":
            return DISPOSITION_LABELS.get(_text(value).lower(), _text(value))
        if key == "trigger":
            return TRIGGER_LABELS.get(_text(value), _text(value))
        if key == "provider":
            return "Credenciais"
        if key == "memberRole":
            return _label_from_map(value, COMMITTEE_ROLE_LABELS)
        if key == "type" and _text(value).lower() in {"institutional", "external"}:
            return "Membro institucional" if _text(value).lower() == "institutional" else "Membro externo"
        if key == "scheduledDatetime":
            return _format_datetime(value)
        if key == "sizeBytes":
            return _format_size(value)
        if key == "availabilityReset":
            return "Sim" if bool(value) else "Não"
        if key in {"courseId", "course"}:
            course = self._course(value)
            if course is not None:
                return _text(course.name)
            if isinstance(value, str) and value:
                return value
            return None
        if key in {"advisorId", "coordinatorUserId"}:
            user = self._user(value)
            if user is not None:
                return _text(user.name) or _text(user.email)
            return None
        if key in {"thesisAuthorNames", "authorNames"} and isinstance(value, (list, tuple)):
            return _join_names(_text(item) for item in value)
        if isinstance(value, bool):
            return "Sim" if value else "Não"
        if isinstance(value, (list, tuple)):
            return ", ".join(_text(item) for item in value if _text(item))
        if isinstance(value, dict):
            return None
        return _text(value)

    def _add_detail(self, entries: list[dict[str, Any]], seen: set[str], label: str, value: Any, before: Any = None, after: Any = None) -> None:
        """
        Append a detail row once, preserving order.

        Args:
            entries: Destination detail rows.
            seen: Labels already present.
            label: Row label.
            value: Row value.
            before: Optional previous value for transitions.
            after: Optional new value for transitions.
        """
        if label in seen:
            return
        value_text = "—" if value is None else _text(value)
        before_text = None if before is None else _text(before)
        after_text = None if after is None else _text(after)
        if value_text in {"", "—"} and not before_text and not after_text:
            return
        seen.add(label)
        entry: dict[str, Any] = {"label": label, "value": value_text}
        if before_text is not None:
            entry["before"] = before_text
        if after_text is not None:
            entry["after"] = after_text
        entries.append(entry)

    def details(self, log: AuditLog) -> list[dict[str, Any]]:
        """
        Build the detail rows shown when an audit entry is opened.

        Args:
            log: Audit entry.

        Returns:
            list[dict[str, Any]]: Friendly label/value rows with optional
                ``before`` and ``after`` values for transitions.
        """
        entries: list[dict[str, Any]] = []
        seen: set[str] = set()
        metadata = self._metadata(log)
        changes = self._changes(log)
        entity_type = log.entity_type
        thesis = self._thesis_for_log(log)
        defense = self._defense_for_log(log)
        member = self._member_for_log(log)
        evaluation = self._evaluation_for_log(log)

        if entity_type == EntityType.COURSE:
            course = self._course(log.entity_id)
            self._add_detail(entries, seen, "Curso", self.entity_name(log))
            if course is not None:
                self._add_detail(entries, seen, "Código", course.code)
                self._add_detail(entries, seen, "Departamento", getattr(course.department, "name", ""))
                self._add_detail(entries, seen, "Coordenador", getattr(course.coordinator, "name", "") if getattr(course, "coordinator_id", None) else course.coordinator_email)
        elif entity_type == EntityType.DEPARTMENT:
            self._add_detail(entries, seen, "Departamento", self.entity_name(log))
        elif entity_type == EntityType.USER_COURSE_ASSIGNMENT:
            user = self._user(log.entity_id)
            self._add_detail(entries, seen, "Usuário", self.entity_name(log))
            if user is not None:
                self._add_detail(entries, seen, "E-mail", user.email)
            self._add_detail(entries, seen, "Curso", self._course_name(log))
        elif entity_type == EntityType.EXTERNAL_INVITATION:
            invitation = self._invitation(log.entity_id)
            self._add_detail(entries, seen, "Convidado", self.entity_name(log))
            self._add_detail(entries, seen, "E-mail", _text(metadata.get("email")) or (invitation.email if invitation is not None else ""))
            if invitation is not None:
                self._add_detail(entries, seen, "Instituição", invitation.institution)
                self._add_detail(entries, seen, "Expira em", _format_datetime(invitation.expires_at))
                self._add_detail(entries, seen, "Situação do convite", _label_from_map(invitation.status, INVITATION_STATUS_LABELS))
        elif entity_type in {EntityType.THESIS, EntityType.THESIS_FILE, EntityType.THESIS_DOCUMENT}:
            if thesis is not None:
                self._add_detail(entries, seen, "TCC", thesis.title)
                self._add_detail(entries, seen, "Código do TCC", thesis.reference_code)
                self._add_detail(entries, seen, "Autor(es)", _join_names(self._author_names(log)))
                self._add_detail(entries, seen, "Orientador", self._advisor_name(log))
                self._add_detail(entries, seen, "Curso", self._course_name(log))
            else:
                self._add_detail(entries, seen, "TCC", self._thesis_title(log))
                self._add_detail(entries, seen, "Código do TCC", self._thesis_reference(log))
                self._add_detail(entries, seen, "Autor(es)", _join_names(self._author_names(log)))
                self._add_detail(entries, seen, "Orientador", self._advisor_name(log))
            if entity_type == EntityType.THESIS_FILE:
                thesis_file = self._thesis_files.get(_uuid(log.entity_id) or "")
                version = thesis_file.version_number if thesis_file is not None else metadata.get("versionNumber")
                self._add_detail(entries, seen, "Versão", version)
                self._add_detail(entries, seen, "Formato", self._format_value(log, "disposition", metadata.get("disposition")))
                file_name = metadata.get("fileName")
                if thesis_file is not None:
                    file_name = thesis_file.stored_file.original_filename
                self._add_detail(entries, seen, "Arquivo", file_name)
            if entity_type == EntityType.THESIS_DOCUMENT:
                document = self._thesis_documents.get(_uuid(log.entity_id) or "")
                document_type = document.type if document is not None else metadata.get("documentType")
                self._add_detail(entries, seen, "Tipo de documento", self._document_type_label(document_type))
                file_name = metadata.get("fileName")
                if document is not None:
                    file_name = document.stored_file.original_filename
                self._add_detail(entries, seen, "Arquivo", file_name)
        elif entity_type == EntityType.DEFENSE:
            if thesis is not None:
                self._add_detail(entries, seen, "TCC", thesis.title)
                self._add_detail(entries, seen, "Código do TCC", thesis.reference_code)
                self._add_detail(entries, seen, "Autor(es)", _join_names(self._author_names(log)))
                self._add_detail(entries, seen, "Orientador", self._advisor_name(log))
                self._add_detail(entries, seen, "Curso", self._course_name(log))
            else:
                self._add_detail(entries, seen, "TCC", self._thesis_title(log))
                self._add_detail(entries, seen, "Código do TCC", self._thesis_reference(log))
                self._add_detail(entries, seen, "Autor(es)", _join_names(self._author_names(log)))
                self._add_detail(entries, seen, "Orientador", self._advisor_name(log))
            if defense is not None:
                self._add_detail(entries, seen, "Data da defesa", _format_datetime(defense.scheduled_datetime))
                self._add_detail(entries, seen, "Status da defesa", _label_from_map(defense.status, DEFENSE_STATUS_LABELS))
            self._add_detail(entries, seen, "Membro da banca", self._member_text(log))
            if evaluation is not None:
                self._add_detail(entries, seen, "Nota", _format_score(evaluation.score))
                self._add_detail(entries, seen, "Decisão", self._decision_label(evaluation.decision))
        elif entity_type == EntityType.REPORT_OVERVIEW:
            self._add_detail(entries, seen, "Relatório", "Visão geral dos TCCs")
            scope = _text(metadata.get("scope"))
            self._add_detail(entries, seen, "Escopo", "Cursos coordenados" if scope == "COORDINATED_COURSES" else "Todos os cursos")
            course = self._course(metadata.get("courseId"))
            if course is not None:
                self._add_detail(entries, seen, "Curso", course.name)
            advisor = self._user(metadata.get("advisorId"))
            if advisor is not None:
                self._add_detail(entries, seen, "Orientador", _text(advisor.name) or _text(advisor.email))
            self._add_detail(entries, seen, "Ano de defesa", metadata.get("year"))

        for source in (changes, metadata):
            for key, value in source.items():
                if entity_type in THESIS_ENTITY_TYPES and key in THESIS_DETAIL_SKIP_KEYS:
                    continue
                if entity_type == EntityType.USER and key in {"name", "email"}:
                    continue
                field = AuditField.parse(key)
                label = FIELD_LABELS.get(field, key) if field is not None else key
                if isinstance(value, dict) and "from" in value and "to" in value:
                    before = self._format_value(log, key, value.get("from"))
                    after = self._format_value(log, key, value.get("to"))
                    if before is None and after is None:
                        continue
                    before_text = before or "—"
                    after_text = after or "—"
                    if before_text == after_text:
                        continue
                    if key in {"roles", "role"}:
                        before_text = ", ".join(role_label(str(item)) or str(item) for item in value.get("from") or []) or "nenhum"
                        after_text = ", ".join(role_label(str(item)) or str(item) for item in value.get("to") or []) or "nenhum"
                    if key == "is_active":
                        before_text = "Ativo" if value.get("from") else "Inativo"
                        after_text = "Ativo" if value.get("to") else "Inativo"
                    self._add_detail(entries, seen, label, f"de {before_text} para {after_text}", before_text, after_text)
                    continue
                formatted = self._format_value(log, key, value)
                if formatted is None:
                    continue
                self._add_detail(entries, seen, label, formatted)
        return entries
