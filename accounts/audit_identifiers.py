"""
Typed identifiers shared by audit writers and the presentation layer.

The enum values are exactly the strings already persisted in ``audit_log`` and
returned by the API, so no data migration or contract change is required. Label
maps are keyed by the enums and live next to their definitions, giving a single
source of truth for each domain concept.
"""
from __future__ import annotations

from enum import StrEnum


class EntityType(StrEnum):
    """Entity types that can be referenced by an audit entry."""

    USER = "User"
    COURSE = "Course"
    DEPARTMENT = "Department"
    USER_COURSE_ASSIGNMENT = "UserCourseAssignment"
    EXTERNAL_INVITATION = "ExternalInvitation"
    SESSION_AUDIT = "SessionAudit"
    AUDIT_LOG = "AuditLog"
    THESIS = "Thesis"
    THESIS_FILE = "ThesisFile"
    THESIS_DOCUMENT = "ThesisDocument"
    DEFENSE = "Defense"
    REPORT_OVERVIEW = "ReportOverview"

    @property
    def app_label(self) -> str:
        """
        Return the Django app label that owns this entity type.

        Returns:
            str: App label stored in ``audit_log.entity_app_label``.
        """
        return _ENTITY_APP_LABELS[self]

    @property
    def label(self) -> str:
        """
        Return the Portuguese label used by the administrative UI.

        Returns:
            str: Human-readable entity label.
        """
        return ENTITY_TYPE_LABELS[self]


_ENTITY_APP_LABELS: dict[EntityType, str] = {
    EntityType.USER: "accounts",
    EntityType.COURSE: "academic",
    EntityType.DEPARTMENT: "academic",
    EntityType.USER_COURSE_ASSIGNMENT: "academic",
    EntityType.EXTERNAL_INVITATION: "accounts",
    EntityType.SESSION_AUDIT: "accounts",
    EntityType.AUDIT_LOG: "accounts",
    EntityType.THESIS: "theses",
    EntityType.THESIS_FILE: "theses",
    EntityType.THESIS_DOCUMENT: "theses",
    EntityType.DEFENSE: "theses",
    EntityType.REPORT_OVERVIEW: "reports",
}

ENTITY_TYPE_LABELS: dict[EntityType, str] = {
    EntityType.USER: "Usuário",
    EntityType.COURSE: "Curso",
    EntityType.DEPARTMENT: "Departamento",
    EntityType.USER_COURSE_ASSIGNMENT: "Vínculo de curso",
    EntityType.EXTERNAL_INVITATION: "Convite externo",
    EntityType.SESSION_AUDIT: "Sessão",
    EntityType.AUDIT_LOG: "Auditoria",
    EntityType.THESIS: "TCC",
    EntityType.THESIS_FILE: "TCC",
    EntityType.THESIS_DOCUMENT: "Documento do TCC",
    EntityType.DEFENSE: "Defesa",
    EntityType.REPORT_OVERVIEW: "Relatório",
}


class AuditEvent(StrEnum):
    """Event codes stored in ``audit_log.metadata['event']``."""

    AUTO_CLOSE_FAILED = "AUTO_CLOSE_FAILED"
    AUTO_STARTED_BY_SCHEDULE = "AUTO_STARTED_BY_SCHEDULE"
    AVAILABILITY_RESPONDED = "AVAILABILITY_RESPONDED"
    DEFENSE_CANCELLED = "DEFENSE_CANCELLED"
    DEFENSE_CLOSED = "DEFENSE_CLOSED"
    DEFENSE_RESULT = "DEFENSE_RESULT"
    DEFENSE_SCHEDULED = "DEFENSE_SCHEDULED"
    DEFENSE_STARTED = "DEFENSE_STARTED"
    DEFENSE_UPDATED = "DEFENSE_UPDATED"
    DOCUMENT_VALIDATED = "DOCUMENT_VALIDATED"
    EVALUATION_BLOCKED_AFTER_CLOSE = "EVALUATION_BLOCKED_AFTER_CLOSE"
    EVALUATION_SUBMITTED = "EVALUATION_SUBMITTED"
    EXTERNAL_TOKEN_REGENERATED = "EXTERNAL_TOKEN_REGENERATED"
    MEMBER_ADDED = "MEMBER_ADDED"
    MEMBER_REMOVED = "MEMBER_REMOVED"
    PUBLISHED = "PUBLISHED"
    STATUS_REVERTED_BY_ADMIN = "STATUS_REVERTED_BY_ADMIN"
    SUBMISSION_WITHDRAWN = "SUBMISSION_WITHDRAWN"
    SUBMITTED = "SUBMITTED"
    VERSION_HISTORY_PURGED = "VERSION_HISTORY_PURGED"


class AuditField(StrEnum):
    """Known keys of ``changes`` and ``metadata`` payloads."""

    NAME = "name"
    EMAIL = "email"
    USER_NAME = "userName"
    USER_EMAIL = "userEmail"
    EVENT = "event"
    SESSION_KEY = "session_key"
    THESIS_ID = "thesisId"
    DEFENSE_ID = "defenseId"
    ROLES = "roles"
    ROLE = "role"
    USER_ID = "userId"
    IS_ACTIVE = "is_active"
    STATUS = "status"
    COURSE = "course"
    COURSE_ID = "courseId"
    COURSE_NAME = "courseName"
    CODE = "code"
    DEPARTMENT = "department"
    DEPARTMENT_ID = "departmentId"
    DEPARTMENT_NAME = "departmentName"
    COORDINATOR_EMAIL = "coordinatorEmail"
    COORDINATOR_EMAIL_LEGACY = "coordinator_email"
    COORDINATOR_USER_ID = "coordinatorUserId"
    COORDINATOR_RESOLVED = "coordinatorResolved"
    INSTITUTION = "institution"
    EXPIRES_AT = "expiresAt"
    SESSION_EXPIRES_AT = "session_expires_at"
    PROVIDER = "provider"
    TITLE = "title"
    THESIS_TITLE = "thesisTitle"
    THESIS_REFERENCE = "thesisReference"
    THESIS_AUTHOR_NAMES = "thesisAuthorNames"
    ADVISOR_NAME = "advisorName"
    ADVISOR_ID = "advisorId"
    VERSION_NUMBER = "versionNumber"
    DISPOSITION = "disposition"
    SHA256 = "sha256"
    SIZE_BYTES = "sizeBytes"
    DOCUMENT_TYPE = "documentType"
    DOCUMENT_ID = "documentId"
    DOCUMENT_NAME = "documentName"
    DECISION = "decision"
    RESULT = "result"
    THESIS_NEW_STATUS = "thesisNewStatus"
    TARGET_STATUS = "targetStatus"
    TRIGGER = "trigger"
    REASON = "reason"
    SCHEDULED_DATETIME = "scheduledDatetime"
    AVAILABILITY_RESET = "availabilityReset"
    MEMBER_ID = "memberId"
    MEMBER_NAME = "memberName"
    MEMBER_ROLE = "memberRole"
    INVITATION_ID = "invitationId"
    PREVIOUS_INVITATION_ID = "previousInvitationId"
    REVIEW_ID = "reviewId"
    REFERENCE_CODE = "referenceCode"
    REMOVED_VERSIONS = "removedVersions"
    KEPT_VERSION = "keptVersion"
    SCOPE = "scope"
    YEAR = "year"
    FORMAT = "format"
    FILE_NAME = "fileName"
    FROM = "from"
    TO = "to"

    @classmethod
    def parse(cls, value: str) -> "AuditField | None":
        """
        Return the enum member matching ``value``, or ``None`` when unknown.

        Args:
            value: Raw field key read from audit metadata or changes.

        Returns:
            AuditField | None: Matching member, or ``None`` for dynamic keys.
        """
        try:
            return cls(value)
        except ValueError:
            return None


FIELD_LABELS: dict[AuditField, str] = {
    AuditField.NAME: "Nome",
    AuditField.EMAIL: "E-mail",
    AuditField.USER_NAME: "Usuário",
    AuditField.USER_EMAIL: "E-mail do usuário",
    AuditField.ROLES: "Perfis",
    AuditField.ROLE: "Função",
    AuditField.IS_ACTIVE: "Status",
    AuditField.STATUS: "Status",
    AuditField.COURSE: "Curso",
    AuditField.COURSE_ID: "Curso",
    AuditField.COURSE_NAME: "Curso",
    AuditField.CODE: "Código",
    AuditField.DEPARTMENT: "Departamento",
    AuditField.DEPARTMENT_ID: "Departamento",
    AuditField.DEPARTMENT_NAME: "Departamento",
    AuditField.COORDINATOR_EMAIL: "Coordenador",
    AuditField.COORDINATOR_EMAIL_LEGACY: "Coordenador",
    AuditField.COORDINATOR_USER_ID: "Coordenador",
    AuditField.COORDINATOR_RESOLVED: "Coordenador definido",
    AuditField.INSTITUTION: "Instituição",
    AuditField.EXPIRES_AT: "Expira em",
    AuditField.SESSION_EXPIRES_AT: "Sessão expira em",
    AuditField.PROVIDER: "Provedor",
    AuditField.TITLE: "Título",
    AuditField.THESIS_TITLE: "TCC",
    AuditField.THESIS_REFERENCE: "Código do TCC",
    AuditField.THESIS_AUTHOR_NAMES: "Autor(es)",
    AuditField.ADVISOR_NAME: "Orientador",
    AuditField.ADVISOR_ID: "Orientador",
    AuditField.VERSION_NUMBER: "Versão",
    AuditField.DISPOSITION: "Formato",
    AuditField.SHA256: "SHA-256",
    AuditField.SIZE_BYTES: "Tamanho",
    AuditField.DOCUMENT_TYPE: "Tipo de documento",
    AuditField.DOCUMENT_ID: "Documento",
    AuditField.DOCUMENT_NAME: "Arquivo",
    AuditField.FILE_NAME: "Arquivo",
    AuditField.DECISION: "Decisão",
    AuditField.RESULT: "Resultado",
    AuditField.THESIS_NEW_STATUS: "Novo status do TCC",
    AuditField.TARGET_STATUS: "Status de destino",
    AuditField.TRIGGER: "Origem do encerramento",
    AuditField.REASON: "Motivo",
    AuditField.SCHEDULED_DATETIME: "Data da defesa",
    AuditField.AVAILABILITY_RESET: "Disponibilidade redefinida",
    AuditField.MEMBER_ID: "Membro da banca",
    AuditField.MEMBER_NAME: "Membro da banca",
    AuditField.MEMBER_ROLE: "Função na banca",
    AuditField.INVITATION_ID: "Convite",
    AuditField.PREVIOUS_INVITATION_ID: "Convite anterior",
    AuditField.REVIEW_ID: "Parecer",
    AuditField.REFERENCE_CODE: "Código do TCC",
    AuditField.REMOVED_VERSIONS: "Versões removidas",
    AuditField.KEPT_VERSION: "Versão mantida",
    AuditField.SCOPE: "Escopo",
    AuditField.YEAR: "Ano",
    AuditField.FORMAT: "Formato",
    AuditField.FROM: "De",
    AuditField.TO: "Para",
}


class AuditCategory(StrEnum):
    """Granular verbosity categories stored in ``audit_log.category``."""

    ACCESS = "ACCESS"
    MANAGEMENT = "MANAGEMENT"
    THESIS = "THESIS"
    PUBLICATION = "PUBLICATION"
    ERROR = "ERROR"

    @property
    def label(self) -> str:
        """
        Return the Portuguese label used by the administrative UI.

        Returns:
            str: Human-readable category label.
        """
        return CATEGORY_LABELS[self]


CATEGORY_LABELS: dict[AuditCategory, str] = {
    AuditCategory.ACCESS: "Acesso",
    AuditCategory.MANAGEMENT: "Gestão",
    AuditCategory.THESIS: "Monografia e defesa",
    AuditCategory.PUBLICATION: "Publicação",
    AuditCategory.ERROR: "Erro",
}
