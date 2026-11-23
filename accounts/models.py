"""
Authentication and audit models for the thesis manager.
"""
from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

from accounts.managers import UserManager


class ProfileRole(models.TextChoices):
    """
    Business roles supported by the application.
    """
    ADMIN = "ADMIN", "Administrador"
    COORDINATOR = "COORDINATOR", "Coordenador"
    ADVISOR = "ADVISOR", "Orientador"
    STUDENT = "STUDENT", "Aluno"


class NetworkOrigin(models.TextChoices):
    """
    Origin of a network request relative to the institution.
    """
    INSTITUTIONAL_NETWORK = "INSTITUTIONAL_NETWORK", "Rede institucional"
    VPN = "VPN", "VPN"
    EXTERNAL = "EXTERNAL", "Externa"


class User(AbstractBaseUser, PermissionsMixin):
    """
    Custom user identified by an institutional e-mail address.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(max_length=254, unique=True)
    name = models.CharField(max_length=200, blank=True)
    registration_number = models.CharField(max_length=30, blank=True, default="", verbose_name="Matrícula")
    department = models.ForeignKey(
        "academic.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="members",
        verbose_name="Departamento",
    )
    research_area = models.CharField(max_length=200, blank=True, default="", verbose_name="Área de pesquisa")
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        db_table = "users_user"
        verbose_name = "Usuário"
        verbose_name_plural = "Usuários"
        constraints = [
            models.UniqueConstraint(
                Lower("email"),
                name="users_user_email_ci_unique",
            ),
        ]

    def __str__(self) -> str:
        return self.name or self.email

    def save(self, *args, **kwargs) -> None:
        if self.email:
            self.email = self.email.strip().lower()
        super().save(*args, **kwargs)

    def clean(self) -> None:
        super().clean()
        if self.email:
            self.email = self.email.strip().lower()

    def get_full_name(self) -> str:
        return self.name or self.email

    def get_short_name(self) -> str:
        return self.name.split(" ", 1)[0] if self.name else self.email

    def active_profile_roles(self):
        """
        Return the roles that are currently assigned to this user.
        """
        return self.profile_roles.filter(revoked_at__isnull=True)

    def has_active_role(self, role: str) -> bool:
        """
        Return whether the user currently owns ``role``.
        """
        return self.active_profile_roles().filter(role=role).exists()


class UserProfileRole(models.Model):
    """
    History of business roles assigned to a user.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="profile_roles",
    )
    role = models.CharField(max_length=20, choices=ProfileRole.choices)
    assigned_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_profile_roles",
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "user_profile_role"
        verbose_name = "Papel de usuário"
        verbose_name_plural = "Papéis de usuário"
        ordering = ["assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"],
                condition=Q(revoked_at__isnull=True),
                name="uniq_active_profile_role_per_user",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "revoked_at"],
                name="user_profile_role_active_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.role}"

    def revoke(self) -> None:
        """
        Mark this role as revoked without deleting its history.
        """
        if self.revoked_at is None:
            self.revoked_at = timezone.now()
            self.save(update_fields=["revoked_at"])


class SessionAudit(models.Model):
    """
    Audit information about authenticated sessions.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session_key = models.CharField(max_length=40, db_index=True)
    user = models.ForeignKey(
        "accounts.User",
        on_delete=models.CASCADE,
        related_name="session_audits",
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    origin = models.CharField(
        max_length=30,
        choices=NetworkOrigin.choices,
        default=NetworkOrigin.EXTERNAL,
    )
    login_at = models.DateTimeField(default=timezone.now)
    logout_at = models.DateTimeField(null=True, blank=True)
    last_activity_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "session_audit"
        verbose_name = "Auditoria de sessão"
        verbose_name_plural = "Auditorias de sessão"
        ordering = ["-login_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session_key", "user"],
                name="uniq_session_audit_session_user",
            ),
        ]
        indexes = [
            models.Index(
                fields=["user", "login_at"],
                name="session_audit_user_login_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.login_at.isoformat()}"


class InvitationStatus(models.TextChoices):
    """
    Lifecycle of an external committee invitation.
    """
    PENDING = "PENDING", "Pendente"
    ACCEPTED = "ACCEPTED", "Aceito"
    DECLINED = "DECLINED", "Recusado"
    EXPIRED = "EXPIRED", "Expirado"
    REVOKED = "REVOKED", "Revogado"
    USED = "USED", "Utilizado"


class ExternalInvitation(models.Model):
    """
    Temporary access invitation for an external committee member.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invited_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sent_external_invitations",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254)
    institution = models.CharField(max_length=200, blank=True)
    token_hash = models.BinaryField(max_length=32, unique=True)
    status = models.CharField(
        max_length=20,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
        db_index=True,
    )
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="revoked_external_invitations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    # Único evento da outbox autorizado a emitir o token por e-mail; eventos
    # anteriores (reentregues ou fora de ordem) são ignorados pelo consumidor.
    token_event_id = models.UUIDField(null=True, blank=True, editable=False)
    token_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "external_invitation"
        verbose_name = "Convite externo"
        verbose_name_plural = "Convites externos"
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["email", "status"],
                name="ext_invite_email_status_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_usable(self) -> bool:
        return (
            self.status in {InvitationStatus.PENDING, InvitationStatus.ACCEPTED}
            and not self.is_expired
            and self.revoked_at is None
        )

    @property
    def has_valid_external_session(self) -> bool:
        """Return whether an exchanged invitation can still back a session."""
        return (
            self.status in {InvitationStatus.ACCEPTED, InvitationStatus.USED}
            and not self.is_expired
            and self.revoked_at is None
        )


class AuditAction(models.TextChoices):
    """
    Actions recorded by the append-only audit log.
    """
    CREATE = "CREATE", "Criação"
    READ = "READ", "Leitura"
    UPDATE = "UPDATE", "Atualização"
    DELETE = "DELETE", "Exclusão"
    LOGIN = "LOGIN", "Login"
    LOGOUT = "LOGOUT", "Logout"
    LOGIN_FAILED = "LOGIN_FAILED", "Falha de login"
    UPLOAD = "UPLOAD", "Upload"
    DOWNLOAD = "DOWNLOAD", "Download"
    REVIEW = "REVIEW", "Revisão"
    SCHEDULE = "SCHEDULE", "Agendamento"
    EVALUATE = "EVALUATE", "Avaliação"
    PUBLISH = "PUBLISH", "Publicação"
    INVITE = "INVITE", "Convite"
    TOKEN_ACCESS = "TOKEN_ACCESS", "Acesso por token"
    APPROVE = "APPROVE", "Aprovação"
    REJECT = "REJECT", "Rejeição"


class AppendOnlyAuditQuerySet(models.QuerySet):
    """Prevent bulk mutation paths from bypassing model-level audit guards."""
    def update(self, **kwargs: object) -> int:
        raise ValidationError("Audit log entries are append-only.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ValidationError("Audit log entries cannot be deleted.")


class AuditLog(models.Model):
    """
    Append-only audit record with a SHA-256 hash chain.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor_user = models.ForeignKey(
        "accounts.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_external_invitation = models.ForeignKey(
        "accounts.ExternalInvitation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
    )
    actor_role = models.CharField(
        max_length=20,
        choices=ProfileRole.choices,
        null=True,
        blank=True,
    )
    actor_roles = models.JSONField(default=list, blank=True)
    category = models.CharField(max_length=20, blank=True, default="")
    action = models.CharField(max_length=30, choices=AuditAction.choices)
    entity_type = models.CharField(max_length=50)
    entity_id = models.CharField(max_length=100)
    entity_app_label = models.CharField(max_length=50)
    origin = models.CharField(
        max_length=30,
        choices=NetworkOrigin.choices,
        default=NetworkOrigin.EXTERNAL,
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    changes = models.JSONField(default=dict, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    previous_hash = models.BinaryField(max_length=32, null=True, blank=True)
    row_hash = models.BinaryField(max_length=32)
    occurred_at = models.DateTimeField(default=timezone.now)
    objects = AppendOnlyAuditQuerySet.as_manager()

    class Meta:
        db_table = "audit_log"
        verbose_name = "Log de auditoria"
        verbose_name_plural = "Logs de auditoria"
        ordering = ["-occurred_at"]
        indexes = [
            models.Index(
                fields=["entity_app_label", "entity_type", "entity_id"],
                name="audit_entity_idx",
            ),
            models.Index(
                fields=["actor_user", "occurred_at"],
                name="audit_actor_user_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action}:{self.entity_type}:{self.entity_id}"

    def save(self, *args, **kwargs) -> None:
        if not self._state.adding:
            raise ValidationError("Audit log entries are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs) -> None:
        raise ValidationError("Audit log entries cannot be deleted.")
