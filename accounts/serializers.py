"""
Serializers for the versioned authentication and administration API.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from rest_framework import serializers

from accounts.audit_presentation import AuditPresenter, CATEGORY_LABELS
from accounts.models import (
    AuditLog,
    ProfileRole,
    User,
)
from accounts.services import (
    ROLE_PRIORITY,
    active_role_codes,
    normalize_role,
    role_label,
)


def _role_codes(user: User) -> list[str]:
    prefetched = getattr(user, "active_role_records", None)
    if prefetched is None:
        return active_role_codes(user)
    assigned = {record.role for record in prefetched}
    return [role for role in ROLE_PRIORITY if role in assigned]


def _active_course(user: User):
    prefetched = getattr(user, "active_course_assignment_records", None)
    if prefetched is not None:
        assignment = prefetched[0] if prefetched else None
        return assignment.course if assignment else None
    try:
        from academic.services import active_course_assignment

        assignment = active_course_assignment(user)
        return assignment.course if assignment else None
    except Exception:
        return None


def _avatar_url(user: User) -> str | None:
    prefetched = getattr(user, "google_social_accounts", None)
    if prefetched is not None:
        account = prefetched[0] if prefetched else None
    else:
        try:
            from allauth.socialaccount.models import SocialAccount

            account = user.socialaccount_set.filter(provider="google").first()
        except Exception:
            account = None
    if account is None:
        return None
    candidate = (getattr(account, "extra_data", None) or {}).get("picture")
    if not isinstance(candidate, str):
        return None
    candidate = candidate.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return candidate


class AdminUserReadSerializer(serializers.ModelSerializer):
    """
    Read serializer matching the user shape consumed by the frontend.
    """
    registrationNumber = serializers.CharField(source="registration_number", read_only=True)
    departmentId = serializers.SerializerMethodField()
    departmentName = serializers.SerializerMethodField()
    researchArea = serializers.CharField(source="research_area", read_only=True)
    role = serializers.SerializerMethodField()
    roleCode = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    avatarUrl = serializers.SerializerMethodField()
    affiliation = serializers.SerializerMethodField()
    course = serializers.SerializerMethodField()
    courseId = serializers.SerializerMethodField()
    lastAccess = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "name",
            "email",
            "registrationNumber",
            "departmentId",
            "departmentName",
            "researchArea",
            "role",
            "roleCode",
            "roles",
            "status",
            "initials",
            "avatarUrl",
            "affiliation",
            "course",
            "courseId",
            "lastAccess",
        ]
        read_only_fields = fields

    def _codes(self, user: User) -> list[str]:
        return _role_codes(user)

    def get_departmentId(self, user: User) -> str | None:
        return str(user.department_id) if user.department_id else None

    def get_departmentName(self, user: User) -> str | None:
        return user.department.name if user.department_id else None

    def get_role(self, user: User) -> str:
        codes = self._codes(user)
        return role_label(codes[0]) if codes else ""

    def get_roleCode(self, user: User) -> str | None:
        codes = self._codes(user)
        return codes[0] if codes else None

    def get_roles(self, user: User) -> list[dict]:
        return [
            {"code": code, "label": role_label(code)}
            for code in self._codes(user)
        ]

    def get_status(self, user: User) -> str:
        return "Ativo" if user.is_active else "Inativo"

    def get_initials(self, user: User) -> str:
        parts = [part for part in (user.name or "").split() if part]
        if parts:
            return "".join(part[0].upper() for part in parts[:2])
        return user.email[:2].upper()

    def get_avatarUrl(self, user: User) -> str | None:
        return _avatar_url(user)

    def get_affiliation(self, user: User) -> str:
        return ""

    def get_course(self, user: User) -> str | None:
        course = _active_course(user)
        return course.name if course else None

    def get_courseId(self, user: User) -> str | None:
        course = _active_course(user)
        return str(course.pk) if course else None

    def get_lastAccess(self, user: User) -> str | None:
        last_access = getattr(user, "last_access", None)
        if last_access is None:
            last_access = (
                user.session_audits.order_by("-last_activity_at")
                .values_list("last_activity_at", flat=True)
                .first()
            )
        return last_access.isoformat() if last_access else None


class AdminUserCreateSerializer(serializers.Serializer):
    """
    Validate administrative user creation payloads.
    """
    name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    registrationNumber = serializers.CharField(max_length=30, required=False, allow_blank=True, default="")
    researchArea = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    departmentId = serializers.UUIDField(required=False, allow_null=True, default=None)
    role = serializers.CharField(required=False, allow_blank=True)
    roles = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=False,
    )
    courseId = serializers.UUIDField(
        required=False,
        allow_null=True,
        write_only=True,
    )

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Informe o nome do usuário.")
        return name

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get("courseId") in ("", "null", None):
            data = {**data, "courseId": None}
        return super().to_internal_value(data)

    def validate(self, attrs: dict) -> dict:
        raw_roles = attrs.pop("roles", None)
        if raw_roles is None:
            raw_roles = []
        singular_role = attrs.pop("role", None)
        if singular_role:
            raw_roles.append(singular_role)
        if not raw_roles:
            raw_roles = [ProfileRole.STUDENT]

        normalized_roles: list[str] = []
        for raw_role in raw_roles:
            normalized = normalize_role(raw_role)
            if not normalized:
                raise serializers.ValidationError(
                    {"role": "Perfil inválido."}
                )
            if normalized not in normalized_roles:
                normalized_roles.append(normalized)
        if ProfileRole.STUDENT in normalized_roles and len(normalized_roles) > 1:
            raise serializers.ValidationError(
                {"roles": "O perfil Aluno não pode ser combinado com outros perfis."}
            )
        attrs["roles"] = normalized_roles
        return attrs


class AdminUserUpdateSerializer(serializers.Serializer):
    """
    Validate partial administrative user updates.
    """
    name = serializers.CharField(max_length=200, required=False)
    email = serializers.EmailField(required=False)
    registrationNumber = serializers.CharField(max_length=30, required=False, allow_blank=True)
    researchArea = serializers.CharField(max_length=200, required=False, allow_blank=True)
    departmentId = serializers.UUIDField(required=False, allow_null=True)
    role = serializers.CharField(required=False, allow_blank=True)
    roles = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=False,
    )
    status = serializers.CharField(required=False)
    is_active = serializers.BooleanField(required=False)
    courseId = serializers.UUIDField(
        required=False,
        allow_null=True,
    )

    def validate_name(self, value: str) -> str:
        raise serializers.ValidationError(
            "O nome do usuário é sincronizado pelo login institucional."
        )

    def validate_email(self, value: str) -> str:
        raise serializers.ValidationError(
            "O e-mail do usuário é sincronizado pelo login institucional."
        )

    def to_internal_value(self, data):
        if isinstance(data, dict) and data.get("courseId") in ("", "null", None):
            data = {**data, "courseId": None}
        return super().to_internal_value(data)

    def validate(self, attrs: dict) -> dict:
        raw_roles = attrs.pop("roles", None)
        singular_role = attrs.pop("role", None)
        if singular_role:
            raw_roles = [singular_role]
        if raw_roles is not None:
            normalized_roles: list[str] = []
            for raw_role in raw_roles:
                normalized = normalize_role(raw_role)
                if not normalized:
                    raise serializers.ValidationError(
                        {"role": "Perfil inválido."}
                    )
                if normalized not in normalized_roles:
                    normalized_roles.append(normalized)
            if ProfileRole.STUDENT in normalized_roles and len(normalized_roles) > 1:
                raise serializers.ValidationError(
                    {"roles": "O perfil Aluno não pode ser combinado com outros perfis."}
                )
            attrs["roles"] = normalized_roles

        if "status" in attrs:
            status = str(attrs.pop("status")).strip().lower()
            if status in {"ativo", "active", "true", "1"}:
                attrs["is_active"] = True
            elif status in {"inativo", "inactive", "false", "0"}:
                attrs["is_active"] = False
            else:
                raise serializers.ValidationError(
                    {"status": "Status inválido."}
                )
        return attrs


class ExternalTokenSerializer(serializers.Serializer):
    """
    Validate external invitation token exchanges.
    """
    token = serializers.CharField(max_length=64)

    def validate_token(self, value: str) -> str:
        token = value.strip().upper()
        if not token:
            raise serializers.ValidationError("Informe o token.")
        return token


class ExternalInvitationCreateSerializer(serializers.Serializer):
    """Validate data for issuing a temporary committee invitation."""
    name = serializers.CharField(max_length=200)
    email = serializers.EmailField()
    institution = serializers.CharField(max_length=200, required=False, allow_blank=True)

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Informe o nome do membro.")
        return name

    def validate_email(self, value: str) -> str:
        return value.strip().lower()


ORIGIN_LABELS = {
    "INSTITUTIONAL_NETWORK": "Rede institucional",
    "VPN": "VPN",
    "EXTERNAL": "Rede externa",
}


def _audit_presenter(context: dict) -> AuditPresenter:
    """
    Return the audit presenter shared by the serializer context.

    Args:
        context: Serializer context mapping.

    Returns:
        AuditPresenter: Presenter primed by the view or an empty fallback.
    """
    presenter = context.get("audit_presenter")
    if presenter is None:
        presenter = AuditPresenter()
        context["audit_presenter"] = presenter
    return presenter


class AuditLogSerializer(serializers.ModelSerializer):
    """
    Read serializer for the administrative audit log.

    Stable codes stored in the table remain available (``actionCode``,
    ``entityType``, ``entityId``, ``changes`` and ``metadata``) for
    compatibility, while the readable fields (``summary``, ``details`` and
    ``entityName``) are produced by :class:`AuditPresenter`.
    """
    timestamp = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()
    actorEmail = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    roleCodes = serializers.SerializerMethodField()
    action = serializers.SerializerMethodField()
    event = serializers.SerializerMethodField()
    actionCode = serializers.SerializerMethodField()
    actionLabel = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    category = serializers.SerializerMethodField()
    categoryLabel = serializers.SerializerMethodField()
    verbosity = serializers.SerializerMethodField()
    verbosityLabel = serializers.SerializerMethodField()
    entityId = serializers.SerializerMethodField()
    entityType = serializers.CharField(source="entity_type", read_only=True)
    entityTypeLabel = serializers.SerializerMethodField()
    entityAppLabel = serializers.CharField(source="entity_app_label", read_only=True)
    entityName = serializers.SerializerMethodField()
    entityNameShort = serializers.SerializerMethodField()
    entityReference = serializers.SerializerMethodField()
    details = serializers.SerializerMethodField()
    origin = serializers.SerializerMethodField()
    originCode = serializers.CharField(source="origin", read_only=True)
    ipAddress = serializers.SerializerMethodField()
    channel = serializers.SerializerMethodField()
    hash = serializers.SerializerMethodField()
    changes = serializers.JSONField(read_only=True)
    metadata = serializers.JSONField(read_only=True)

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "timestamp",
            "actor",
            "actorEmail",
            "role",
            "roles",
            "roleCodes",
            "action",
            "event",
            "actionCode",
            "actionLabel",
            "summary",
            "category",
            "categoryLabel",
            "verbosity",
            "verbosityLabel",
            "entityType",
            "entityTypeLabel",
            "entityAppLabel",
            "entityId",
            "entityName",
            "entityNameShort",
            "entityReference",
            "details",
            "origin",
            "originCode",
            "ipAddress",
            "channel",
            "hash",
            "changes",
            "metadata",
        ]
        read_only_fields = fields

    def get_timestamp(self, log: AuditLog) -> str:
        return log.occurred_at.isoformat()

    def get_actor(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).actor_name(log)

    def get_actorEmail(self, log: AuditLog) -> str | None:
        return _audit_presenter(self.context).actor_email(log)

    def get_role(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).role_text(log)

    def get_roles(self, log: AuditLog) -> list[dict[str, str]]:
        return _audit_presenter(self.context).roles(log)

    def get_roleCodes(self, log: AuditLog) -> list[str]:
        return _audit_presenter(self.context).role_codes(log)

    def get_event(self, log: AuditLog) -> dict[str, object]:
        return _audit_presenter(self.context).structured(log).as_dict()

    def get_actionCode(self, log: AuditLog) -> str:
        return log.action

    def get_actionLabel(self, log: AuditLog) -> str:
        return log.get_action_display()

    def get_summary(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).summary(log)

    def get_category(self, log: AuditLog) -> str:
        return log.category or ""

    def get_categoryLabel(self, log: AuditLog) -> str:
        return CATEGORY_LABELS.get(log.category, log.category or "—")

    def get_verbosity(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).verbosity(log)

    def get_verbosityLabel(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).verbosity_label(log)

    def get_entityTypeLabel(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).entity_type_label(log)

    def get_entityName(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).entity_name(log)

    def get_entityNameShort(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).entity_name_short(log)

    def get_entityReference(self, log: AuditLog) -> str:
        return _audit_presenter(self.context).entity_reference(log)

    def get_details(self, log: AuditLog) -> list[dict[str, object]]:
        return _audit_presenter(self.context).details(log)

    def get_action(self, log: AuditLog) -> str:
        return self.get_summary(log)

    def get_entityId(self, log: AuditLog) -> str:
        return log.entity_id

    def get_origin(self, log: AuditLog) -> str:
        return ORIGIN_LABELS.get(log.origin, log.get_origin_display())

    def get_ipAddress(self, log: AuditLog) -> str | None:
        if not log.ip_address:
            return None
        return str(log.ip_address)

    def get_channel(self, log: AuditLog) -> str:
        if log.actor_external_invitation_id:
            return "Acesso por token externo"
        if log.actor_user_id:
            return "Sistema web (usuário autenticado)"
        return "Sistema"

    def get_hash(self, log: AuditLog) -> str:
        return bytes(log.row_hash).hex()[:8]
