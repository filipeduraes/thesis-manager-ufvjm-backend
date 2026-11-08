"""
Serializers for the versioned authentication and administration API.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from rest_framework import serializers

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


ENTITY_TYPE_LABELS = {
    "User": "usuário",
    "Course": "curso",
    "Department": "departamento",
    "UserCourseAssignment": "vínculo de curso",
    "ExternalInvitation": "convite externo",
    "SessionAudit": "sessão",
    "AuditLog": "auditoria",
}

ACTION_VERBS = {
    "CREATE": "Criou",
    "READ": "Consultou",
    "UPDATE": "Atualizou",
    "DELETE": "Excluiu",
    "LOGIN": "Realizou login",
    "LOGOUT": "Realizou logout",
    "LOGIN_FAILED": "Falha de login",
    "UPLOAD": "Enviou arquivo",
    "DOWNLOAD": "Baixou arquivo",
    "REVIEW": "Revisou",
    "SCHEDULE": "Agendou",
    "EVALUATE": "Avaliou",
    "PUBLISH": "Publicou",
    "INVITE": "Convidou",
    "TOKEN_ACCESS": "Acessou por token",
    "APPROVE": "Aprovou",
    "REJECT": "Rejeitou",
}

CHANGE_LABELS = {
    "name": "nome",
    "email": "e-mail",
    "userName": "usuário",
    "userEmail": "e-mail do usuário",
    "roles": "perfis",
    "role": "perfil",
    "is_active": "status",
    "status": "status",
    "courseId": "curso",
    "course": "curso",
    "code": "código",
    "department": "departamento",
    "departmentId": "departamento",
    "departmentName": "departamento",
    "coordinatorEmail": "coordenador",
    "coordinator_email": "coordenador",
    "coordinator": "coordenador",
    "institution": "instituição",
    "expiresAt": "expiração",
}


def _role_labels(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    labels: list[str] = []
    for item in values:
        label = role_label(str(item)) or str(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def _status_label(value: object) -> str | None:
    if value in {True, "Ativo", "ativo", "active"}:
        return "Ativo"
    if value in {False, "Inativo", "inativo", "inactive"}:
        return "Inativo"
    if value in (None, ""):
        return None
    return str(value)


def _describe_transition(key: str, value: dict) -> str | None:
    before = value.get("from")
    after = value.get("to")
    if key in {"roles", "role"}:
        from_labels = _role_labels(before)
        to_labels = _role_labels(after)
        from_text = ", ".join(from_labels) or "nenhum"
        to_text = ", ".join(to_labels) or "nenhum"
        if from_text == to_text:
            return f"perfis: {to_text}"
        return f"perfis: de {from_text} para {to_text}"
    if key in {"is_active", "status"}:
        from_text = _status_label(before) or "—"
        to_text = _status_label(after) or "—"
        if from_text == to_text:
            return f"status: {to_text}"
        return f"status: de {from_text} para {to_text}"
    if key in {"course"}:
        from_text = str(before) if before not in (None, "") else "sem curso"
        to_text = str(after) if after not in (None, "") else "sem curso"
        if from_text == to_text:
            return f"curso: {to_text}"
        return f"curso: de {from_text} para {to_text}"
    return None


def _describe_changes(log: AuditLog) -> str:
    changes = log.changes if isinstance(log.changes, dict) else {}
    metadata = log.metadata if isinstance(log.metadata, dict) else {}
    payload = {**metadata, **changes}
    if not payload:
        return ""
    parts: list[str] = []
    if log.entity_type == "UserCourseAssignment":
        user_name = payload.get("userName") or payload.get("userEmail")
        if user_name:
            parts.append(f"usuário: {user_name}")
        course_value = payload.get("course")
        if isinstance(course_value, dict):
            detail = _describe_transition("course", course_value)
            if detail:
                parts.append(detail)
        else:
            course_name = course_value
            course_id = payload.get("courseId")
            if course_name is None and course_id in (None, "", False) and ("course" in payload or "courseId" in payload):
                parts.append("curso removido")
            elif isinstance(course_name, str) and course_name:
                parts.append(f"curso: {course_name}")
            elif course_id:
                parts.append(f"curso: {course_id}")
        return "; ".join(parts)

    for key, value in payload.items():
        label = CHANGE_LABELS.get(key, key)
        if key in {"userName", "userEmail"} and log.entity_type == "User":
            continue
        if key in {"name", "email"} and log.entity_type == "User" and key in metadata and key not in changes:
            continue
        if isinstance(value, dict) and "from" in value and "to" in value:
            detail = _describe_transition(key, value)
            if detail and detail not in parts:
                parts.append(detail)
            continue
        if key in {"roles", "role"} and isinstance(value, (list, tuple)):
            labels = _role_labels(value)
            parts.append(f"perfis: {', '.join(labels)}" if labels else "perfis")
            continue
        if key in {"is_active", "status"}:
            status = _status_label(value)
            if status:
                parts.append(f"status: {status}")
            else:
                parts.append(f"{label}: {value}")
            continue
        if key in {"email", "coordinatorEmail", "coordinator_email", "userEmail"} and value:
            parts.append(f"{label}: {value}")
            continue
        if key in {"name", "code", "institution", "userName"} and value:
            parts.append(f"{label}: {value}")
            continue
        if key in {"courseId", "course", "departmentId", "department"} and value in (None, "", False):
            if key in {"courseId", "course"}:
                parts.append("curso removido")
            else:
                parts.append(f"{label} removido")
            continue
        if key in {"course", "department", "departmentName"} and isinstance(value, str) and value:
            parts.append(f"{label}: {value}")
            continue
        if key == "courseId" and value:
            continue
        if label not in {"perfis"} and label not in parts:
            parts.append(label)
    # Remove duplicatas preservando ordem.
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        unique.append(part)
    return "; ".join(unique[:6])


ORIGIN_LABELS = {
    "INSTITUTIONAL_NETWORK": "Rede institucional",
    "VPN": "VPN",
    "EXTERNAL": "Rede externa",
}


class AuditLogSerializer(serializers.ModelSerializer):
    """
    Read serializer for the administrative audit log.
    """
    timestamp = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    action = serializers.SerializerMethodField()
    actionCode = serializers.SerializerMethodField()
    actionLabel = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    entityId = serializers.SerializerMethodField()
    entityType = serializers.CharField(source="entity_type", read_only=True)
    entityAppLabel = serializers.CharField(source="entity_app_label", read_only=True)
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
            "role",
            "action",
            "actionCode",
            "actionLabel",
            "summary",
            "entityType",
            "entityAppLabel",
            "entityId",
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
        if log.actor_user_id:
            return log.actor_user.name or log.actor_user.email
        if log.actor_external_invitation_id:
            return log.actor_external_invitation.name
        return "Sistema"

    def get_role(self, log: AuditLog) -> str:
        return role_label(log.actor_role) or "Sistema"

    def get_actionCode(self, log: AuditLog) -> str:
        return log.action

    def get_actionLabel(self, log: AuditLog) -> str:
        return log.get_action_display()

    def get_summary(self, log: AuditLog) -> str:
        changes = log.changes if isinstance(log.changes, dict) else {}
        metadata = log.metadata if isinstance(log.metadata, dict) else {}
        if log.entity_type == "User" and log.action == "UPDATE":
            name = metadata.get("name") or metadata.get("email") or f"ID {log.entity_id}"
            role_change = changes.get("roles")
            if isinstance(role_change, dict):
                from_labels = _role_labels(role_change.get("from"))
                to_labels = _role_labels(role_change.get("to"))
                from_text = ", ".join(from_labels) or "nenhum"
                to_text = ", ".join(to_labels) or "nenhum"
                noun = "perfil" if len(to_labels) <= 1 and len(from_labels) <= 1 else "perfis"
                base = f"Atualizou {noun} do usuário {name} de {from_text} para {to_text}"
                extra_parts = []
                course_change = changes.get("course")
                if isinstance(course_change, dict):
                    detail = _describe_transition("course", course_change)
                    if detail:
                        extra_parts.append(detail)
                status_change = changes.get("is_active") or changes.get("status")
                if isinstance(status_change, dict):
                    detail = _describe_transition("is_active", status_change)
                    if detail:
                        extra_parts.append(detail)
                if extra_parts:
                    return f"{base} ({'; '.join(extra_parts)})"
                return base
        entity = ENTITY_TYPE_LABELS.get(log.entity_type, log.entity_type.lower())
        details = _describe_changes(log)
        verb = ACTION_VERBS.get(log.action, log.get_action_display())
        base = f"{verb} {entity}"
        if not details and log.entity_type == "UserCourseAssignment":
            details = f"usuário ID: {log.entity_id}"
        if details:
            return f"{base} ({details})"
        return base

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
