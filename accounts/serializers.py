"""
Serializers for the versioned authentication and administration API.
"""
from __future__ import annotations

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


class AdminUserReadSerializer(serializers.ModelSerializer):
    """
    Read serializer matching the user shape consumed by the frontend.
    """
    role = serializers.SerializerMethodField()
    roleCode = serializers.SerializerMethodField()
    profile = serializers.SerializerMethodField()
    roles = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    initials = serializers.SerializerMethodField()
    affiliation = serializers.SerializerMethodField()
    course = serializers.SerializerMethodField()
    lastAccess = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id",
            "name",
            "email",
            "role",
            "roleCode",
            "profile",
            "roles",
            "status",
            "initials",
            "affiliation",
            "course",
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

    def get_profile(self, user: User) -> str:
        return self.get_role(user)

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

    def get_affiliation(self, user: User) -> str:
        return ""

    def get_course(self, user: User) -> str | None:
        return None

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
    courseId = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
    )

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Informe o nome do usuário.")
        return name

    def validate_email(self, value: str) -> str:
        return value.strip().lower()

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
        attrs["roles"] = normalized_roles
        return attrs


class AdminUserUpdateSerializer(serializers.Serializer):
    """
    Validate partial administrative user updates.
    """
    name = serializers.CharField(max_length=200, required=False)
    email = serializers.EmailField(required=False, read_only=True)
    role = serializers.CharField(required=False, allow_blank=True)
    roles = serializers.ListField(
        child=serializers.CharField(),
        required=False,
        allow_empty=False,
    )
    status = serializers.CharField(required=False)
    is_active = serializers.BooleanField(required=False)
    courseId = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
    )

    def validate_name(self, value: str) -> str:
        name = value.strip()
        if not name:
            raise serializers.ValidationError("Informe o nome do usuário.")
        return name

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


class ActiveProfileSerializer(serializers.Serializer):
    """
    Validate active profile switching requests.
    """
    profile = serializers.CharField(required=False, allow_blank=True)
    role = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs: dict) -> dict:
        raw_role = attrs.get("role") or attrs.get("profile")
        normalized = normalize_role(raw_role)
        if not normalized:
            raise serializers.ValidationError(
                {"profile": "Perfil inválido."}
            )
        attrs["role"] = normalized
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


class AuditLogSerializer(serializers.ModelSerializer):
    """
    Read serializer for the administrative audit log.
    """
    timestamp = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()
    role = serializers.SerializerMethodField()
    action = serializers.SerializerMethodField()
    entityId = serializers.SerializerMethodField()
    entityType = serializers.CharField(source="entity_type", read_only=True)
    origin = serializers.SerializerMethodField()
    hash = serializers.SerializerMethodField()

    class Meta:
        model = AuditLog
        fields = [
            "id",
            "timestamp",
            "actor",
            "role",
            "action",
            "entityType",
            "entityId",
            "origin",
            "hash",
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

    def get_action(self, log: AuditLog) -> str:
        return log.get_action_display()

    def get_entityId(self, log: AuditLog) -> str:
        return log.entity_id

    def get_origin(self, log: AuditLog) -> str:
        return log.get_origin_display()

    def get_hash(self, log: AuditLog) -> str:
        return bytes(log.row_hash).hex()[:8]
