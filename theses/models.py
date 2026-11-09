"""
Models for the thesis submission, review and publication workflow.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from academic.models import Course


class ThesisType(models.TextChoices):
    """
    Academic work types supported by the thesis domain.
    """

    MONOGRAPH = "MONOGRAPH", "Monografia"
    ARTICLE = "ARTICLE", "Artigo"


class ThesisStatus(models.TextChoices):
    """
    Lifecycle of a thesis submission.
    """

    DRAFT = "DRAFT", "Rascunho"
    SUBMITTED = "SUBMITTED", "Submetida"
    ADVISOR_REVIEW = "ADVISOR_REVIEW", "Em revisão do orientador"
    CHANGES_REQUESTED = "CHANGES_REQUESTED", "Ajustes solicitados"
    ADVISOR_APPROVED = "ADVISOR_APPROVED", "Aprovada pelo orientador"
    READY_FOR_DEFENSE = "READY_FOR_DEFENSE", "Pronta para banca"
    DEFENSE_SCHEDULED = "DEFENSE_SCHEDULED", "Banca agendada"
    UNDER_EVALUATION = "UNDER_EVALUATION", "Em avaliação pela banca"
    APPROVED = "APPROVED", "Aprovada"
    APPROVED_WITH_REMARKS = "APPROVED_WITH_REMARKS", "Aprovada com ressalvas"
    REJECTED = "REJECTED", "Reprovada"
    CORRECTIONS_PENDING = "CORRECTIONS_PENDING", "Correções pendentes"
    PUBLISHED = "PUBLISHED", "Publicada"
    ARCHIVED = "ARCHIVED", "Arquivada"


class ReviewDecision(models.TextChoices):
    """
    Advisor decisions for a submitted thesis version.
    """

    APPROVED = "APPROVED", "Aprovada"
    CHANGES_REQUESTED = "CHANGES_REQUESTED", "Ajustes solicitados"
    REJECTED = "REJECTED", "Reprovada"


class ProcessingStatus(models.TextChoices):
    """
    Processing state of an uploaded file.
    """

    PENDING = "PENDING", "Pendente"
    PROCESSING = "PROCESSING", "Processando"
    COMPLETED = "COMPLETED", "Concluído"
    FAILED = "FAILED", "Falhou"
    RETRYING = "RETRYING", "Reprocessando"


class DocumentType(models.TextChoices):
    """
    Supplementary document types accepted by the thesis workflow.
    """

    DEFENSE_MINUTES = "DEFENSE_MINUTES", "Ata de defesa"
    PUBLICATION_AUTHORIZATION = "PUBLICATION_AUTHORIZATION", "Autorização de publicação"
    SPELLING_CHECK_PROOF = "SPELLING_CHECK_PROOF", "Comprovante de revisão"
    OTHER = "OTHER", "Outro"


class DocumentStatus(models.TextChoices):
    """
    Validation state of a supplementary document.
    """

    PENDING = "PENDING", "Pendente"
    SUBMITTED = "SUBMITTED", "Enviado"
    VALIDATED = "VALIDATED", "Validado"
    REJECTED = "REJECTED", "Rejeitado"


class DefenseResult(models.TextChoices):
    """
    Final defense results recorded for a thesis.
    """

    APPROVED = "APPROVED", "Aprovada"
    APPROVED_WITH_REMARKS = "APPROVED_WITH_REMARKS", "Aprovada com ressalvas"
    REJECTED = "REJECTED", "Reprovada"


class DefenseStatus(models.TextChoices):
    """
    Lifecycle of a thesis defense session.
    """

    SCHEDULED = "SCHEDULED", "Agendada"
    IN_PROGRESS = "IN_PROGRESS", "Em andamento"
    COMPLETED = "COMPLETED", "Realizada"
    CANCELLED = "CANCELLED", "Cancelada"


class DefenseModality(models.TextChoices):
    """
    Format of a thesis defense: in-person or remote.
    """

    IN_PERSON = "IN_PERSON", "Presencial"
    REMOTE = "REMOTE", "Remota"


class CommitteeRole(models.TextChoices):
    """
    Role of a member in a defense committee.
    """

    PRESIDENT = "PRESIDENT", "Presidente"
    MEMBER = "MEMBER", "Membro"
    ALTERNATE = "ALTERNATE", "Suplente"


EDITABLE_STATUSES = (
    ThesisStatus.DRAFT,
    ThesisStatus.CHANGES_REQUESTED,
    ThesisStatus.REJECTED,
)

REVIEWABLE_STATUSES = (
    ThesisStatus.SUBMITTED,
    ThesisStatus.ADVISOR_REVIEW,
)

DEFENSE_RESULT_STATUSES = (
    ThesisStatus.ADVISOR_APPROVED,
    ThesisStatus.READY_FOR_DEFENSE,
    ThesisStatus.DEFENSE_SCHEDULED,
    ThesisStatus.UNDER_EVALUATION,
)

PUBLISHABLE_STATUSES = (
    ThesisStatus.APPROVED,
    ThesisStatus.APPROVED_WITH_REMARKS,
)

APPROVED_DEFENSE_STATUSES = (
    ThesisStatus.APPROVED,
    ThesisStatus.APPROVED_WITH_REMARKS,
)

SCHEDULABLE_STATUSES = (
    ThesisStatus.ADVISOR_APPROVED,
    ThesisStatus.READY_FOR_DEFENSE,
)

ACTIVE_DEFENSE_STATUSES = (
    DefenseStatus.SCHEDULED,
    DefenseStatus.IN_PROGRESS,
)


def build_reference_code() -> str:
    """
    Build a unique, human-readable thesis reference code.

    Returns:
        str: Reference code in the ``TCC-<year>-<random>`` format.
    """
    year = date.today().year
    suffix = secrets.token_hex(3).upper()
    return f"TCC-{year}-{suffix}"


class Keyword(models.Model):
    """
    Search keyword associated with one or more theses.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=50)
    normalized_name = models.CharField(max_length=50, unique=True)

    class Meta:
        """
        Database table and ordering configuration for keywords.
        """

        db_table = "keyword"
        verbose_name = "Palavra-chave"
        verbose_name_plural = "Palavras-chave"
        ordering = ["name"]

    def __str__(self) -> str:
        """
        Return the human-readable keyword.

        Returns:
            str: Keyword name.
        """
        return self.name

    def save(self, *args, **kwargs) -> None:
        """
        Normalize the keyword before saving.

        Args:
            *args: Positional arguments forwarded to Django's save.
            **kwargs: Keyword arguments forwarded to Django's save.
        """
        if self.name:
            self.name = " ".join(self.name.split())
            self.normalized_name = self.name.casefold()
        super().save(*args, **kwargs)


class StoredFile(models.Model):
    """
    Physical file stored by the application, deduplicated by SHA-256.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    original_filename = models.CharField(max_length=255)
    stored_filename = models.CharField(max_length=255, unique=True)
    mime_type = models.CharField(max_length=100, default="application/pdf")
    extension = models.CharField(max_length=10, default=".pdf")
    size_bytes = models.BigIntegerField()
    sha256 = models.BinaryField(max_length=32, unique=True)
    storage_path = models.CharField(max_length=512)
    storage_bucket = models.CharField(max_length=100, blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stored_files",
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    processing_status = models.CharField(
        max_length=20,
        choices=ProcessingStatus.choices,
        default=ProcessingStatus.PENDING,
        db_index=True,
    )
    extracted_text_path = models.CharField(max_length=512, blank=True, default="")
    extracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """
        Database table, ordering and indexes for stored files.
        """

        db_table = "stored_file"
        verbose_name = "Arquivo armazenado"
        verbose_name_plural = "Arquivos armazenados"
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(
                fields=["processing_status", "uploaded_at"],
                name="stored_file_processing_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the stored file name.

        Returns:
            str: Stored filename.
        """
        return self.stored_filename

    @property
    def sha256_hex(self) -> str:
        """
        Return the SHA-256 digest as a hexadecimal string.

        Returns:
            str: Hexadecimal digest.
        """
        return bytes(self.sha256).hex()

    def clean(self) -> None:
        """
        Validate file metadata against the domain rules.

        Raises:
            ValidationError: If the file is not a PDF or exceeds the size limit.
        """
        super().clean()
        if self.mime_type != "application/pdf":
            raise ValidationError("Apenas arquivos PDF são permitidos.")
        if self.size_bytes <= 0:
            raise ValidationError("O arquivo enviado está vazio.")


class Thesis(models.Model):
    """
    Undergraduate thesis submitted by a student and advised by a professor.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="theses",
    )
    advisor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="advised_theses",
    )
    type = models.CharField(
        max_length=20,
        choices=ThesisType.choices,
        default=ThesisType.MONOGRAPH,
    )
    title = models.CharField(max_length=500)
    abstract = models.TextField(blank=True, default="")
    journal = models.CharField(max_length=200, blank=True, default="")
    reference_code = models.CharField(max_length=50, unique=True, editable=False)
    status = models.CharField(
        max_length=30,
        choices=ThesisStatus.choices,
        default=ThesisStatus.DRAFT,
        db_index=True,
    )
    is_public = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    advisor_approved_at = models.DateTimeField(null=True, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """
        Database table, ordering and indexes for theses.
        """

        db_table = "thesis"
        verbose_name = "Monografia"
        verbose_name_plural = "Monografias"
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["status", "updated_at"], name="thesis_status_updated_idx"),
            models.Index(fields=["advisor", "status"], name="thesis_advisor_status_idx"),
            models.Index(fields=["course", "status"], name="thesis_course_status_idx"),
            models.Index(fields=["is_public", "published_at"], name="thesis_public_idx"),
        ]

    def __str__(self) -> str:
        """
        Return a human-readable thesis representation.

        Returns:
            str: Thesis title and reference code.
        """
        return f"{self.title} ({self.reference_code})"

    def save(self, *args, **kwargs) -> None:
        """
        Normalize fields and guarantee a unique reference code.

        Args:
            *args: Positional arguments forwarded to Django's save.
            **kwargs: Keyword arguments forwarded to Django's save.
        """
        if self.title:
            self.title = " ".join(self.title.split())
        if not self.reference_code:
            candidate = build_reference_code()
            while Thesis.objects.filter(reference_code=candidate).exists():
                candidate = build_reference_code()
            self.reference_code = candidate
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs) -> None:
        """
        Prevent deletion of published theses through ordinary CRUD paths.

        Raises:
            ValidationError: If the thesis is published.
        """
        if self.status == ThesisStatus.PUBLISHED or self.is_public:
            raise ValidationError("Monografias publicadas não podem ser excluídas.")
        super().delete(*args, **kwargs)

    @property
    def current_file(self) -> ThesisFile | None:
        """
        Return the current thesis file, if any.

        Returns:
            ThesisFile | None: Current version.
        """
        return self.files.filter(is_current=True).first()

    @property
    def main_author(self) -> ThesisAuthor | None:
        """
        Return the main author of the thesis.

        Returns:
            ThesisAuthor | None: Main author record.
        """
        return self.authors.filter(is_main_author=True).first()


class ThesisAuthor(models.Model):
    """
    Author association between a user and a thesis.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="authors",
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="authored_theses",
    )
    position = models.PositiveSmallIntegerField(default=1)
    is_main_author = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """
        Database table and constraints for thesis authors.
        """

        db_table = "thesis_author"
        verbose_name = "Autor da monografia"
        verbose_name_plural = "Autores da monografia"
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["thesis", "position"],
                name="uniq_thesis_author_position",
            ),
            models.UniqueConstraint(
                fields=["thesis", "author"],
                name="uniq_thesis_author_user",
            ),
            models.UniqueConstraint(
                fields=["thesis"],
                condition=Q(is_main_author=True),
                name="uniq_thesis_main_author",
            ),
            models.CheckConstraint(
                condition=Q(position__gte=1),
                name="thesis_author_position_gte_1",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the author and thesis identifiers.

        Returns:
            str: Author representation.
        """
        return f"{self.author_id}:{self.thesis_id}"


class ThesisKeyword(models.Model):
    """
    Association between a thesis and a keyword.
    """

    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="keyword_links",
    )
    keyword = models.ForeignKey(
        Keyword,
        on_delete=models.PROTECT,
        related_name="thesis_links",
    )

    class Meta:
        """
        Database table and uniqueness configuration for thesis keywords.
        """

        db_table = "thesis_keyword"
        verbose_name = "Palavra-chave da monografia"
        verbose_name_plural = "Palavras-chave da monografia"
        constraints = [
            models.UniqueConstraint(
                fields=["thesis", "keyword"],
                name="uniq_thesis_keyword",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the thesis and keyword identifiers.

        Returns:
            str: Association representation.
        """
        return f"{self.thesis_id}:{self.keyword_id}"


class ThesisFile(models.Model):
    """
    Version of a thesis PDF linked to a stored file.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="files",
    )
    stored_file = models.ForeignKey(
        StoredFile,
        on_delete=models.PROTECT,
        related_name="thesis_files",
    )
    version_number = models.PositiveSmallIntegerField()
    is_current = models.BooleanField(default=False, db_index=True)
    change_description = models.TextField(blank=True, default="")
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="uploaded_thesis_files",
    )
    uploaded_at = models.DateTimeField(default=timezone.now)
    hash_verified_at = models.DateTimeField(null=True, blank=True)
    text_extracted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """
        Database table, constraints and indexes for thesis files.
        """

        db_table = "thesis_file"
        verbose_name = "Versão da monografia"
        verbose_name_plural = "Versões da monografia"
        ordering = ["-version_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["thesis", "version_number"],
                name="uniq_thesis_file_version",
            ),
            models.UniqueConstraint(
                fields=["thesis"],
                condition=Q(is_current=True),
                name="uniq_thesis_current_file",
            ),
        ]
        indexes = [
            models.Index(
                fields=["thesis", "is_current"],
                name="thesis_file_current_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the thesis version representation.

        Returns:
            str: Thesis identifier and version number.
        """
        return f"{self.thesis_id}:v{self.version_number}"


class ThesisReview(models.Model):
    """
    Advisor review recorded for a specific thesis version.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis_file = models.ForeignKey(
        ThesisFile,
        on_delete=models.CASCADE,
        related_name="reviews",
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="thesis_reviews",
    )
    decision = models.CharField(max_length=30, choices=ReviewDecision.choices)
    comments = models.TextField(blank=True, default="")
    recommendations = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(default=timezone.now)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        """
        Database table and ordering configuration for thesis reviews.
        """

        db_table = "thesis_review"
        verbose_name = "Parecer da monografia"
        verbose_name_plural = "Pareceres da monografia"
        ordering = ["-reviewed_at"]
        indexes = [
            models.Index(
                fields=["thesis_file", "reviewed_at"],
                name="thesis_review_file_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the review decision and file identifier.

        Returns:
            str: Review representation.
        """
        return f"{self.thesis_file_id}:{self.decision}"


class ThesisStatusHistory(models.Model):
    """
    Immutable history of thesis status transitions.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="status_history",
    )
    previous_status = models.CharField(
        max_length=30,
        choices=ThesisStatus.choices,
        blank=True,
        default="",
    )
    new_status = models.CharField(max_length=30, choices=ThesisStatus.choices)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="thesis_status_changes",
    )
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        """
        Database table, ordering and indexes for thesis status history.
        """

        db_table = "thesis_status_history"
        verbose_name = "Histórico de status da monografia"
        verbose_name_plural = "Históricos de status da monografia"
        ordering = ["-changed_at"]
        indexes = [
            models.Index(
                fields=["thesis", "changed_at"],
                name="thesis_status_history_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the transition representation.

        Returns:
            str: Previous and new statuses.
        """
        return f"{self.previous_status}->{self.new_status}"


class ThesisDocument(models.Model):
    """
    Supplementary document attached to a thesis workflow.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    thesis_file = models.ForeignKey(
        ThesisFile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="documents",
    )
    stored_file = models.ForeignKey(
        StoredFile,
        on_delete=models.PROTECT,
        related_name="thesis_documents",
    )
    type = models.CharField(max_length=40, choices=DocumentType.choices)
    status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.SUBMITTED,
        db_index=True,
    )
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="submitted_thesis_documents",
    )
    validated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="validated_thesis_documents",
    )
    submitted_at = models.DateTimeField(default=timezone.now)
    validated_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")

    class Meta:
        """
        Database table, ordering and indexes for supplementary documents.
        """

        db_table = "thesis_document"
        verbose_name = "Documento da monografia"
        verbose_name_plural = "Documentos da monografia"
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(
                fields=["thesis", "type", "status"],
                name="thesis_document_type_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return the document type and thesis identifier.

        Returns:
            str: Document representation.
        """
        return f"{self.type}:{self.thesis_id}"


class Defense(models.Model):
    """
    Scheduled defense session for an advisor-approved thesis.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thesis = models.ForeignKey(
        Thesis,
        on_delete=models.PROTECT,
        related_name="defenses",
    )
    scheduled_datetime = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=120)
    modality = models.CharField(
        max_length=20,
        choices=DefenseModality.choices,
        default=DefenseModality.IN_PERSON,
    )
    location = models.CharField(max_length=500, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=DefenseStatus.choices,
        default=DefenseStatus.SCHEDULED,
        db_index=True,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scheduled_defenses",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """
        Database table, constraints and indexes for defense sessions.
        """

        db_table = "defense"
        verbose_name = "Defesa"
        verbose_name_plural = "Defesas"
        ordering = ["-scheduled_datetime"]
        constraints = [
            # Apenas uma defesa ativa (agendada ou em andamento) por monografia.
            models.UniqueConstraint(
                fields=["thesis"],
                condition=Q(status__in=["SCHEDULED", "IN_PROGRESS"]),
                name="uniq_active_defense_per_thesis",
            ),
        ]
        indexes = [
            models.Index(fields=["thesis", "status"], name="defense_thesis_status_idx"),
            models.Index(fields=["scheduled_datetime", "status"], name="defense_datetime_idx"),
        ]

    def __str__(self) -> str:
        """
        Return the defense thesis and scheduled date.

        Returns:
            str: Thesis identifier and scheduled date.
        """
        return f"{self.thesis_id}@{self.scheduled_datetime.date()}"


class DefenseMember(models.Model):
    """
    Committee member assigned to a thesis defense.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    defense = models.ForeignKey(
        Defense,
        on_delete=models.CASCADE,
        related_name="members",
    )
    role = models.CharField(max_length=20, choices=CommitteeRole.choices)
    institutional_member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="committee_memberships",
    )
    external_invitation = models.OneToOneField(
        "accounts.ExternalInvitation",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="defense_membership",
    )
    name = models.CharField(max_length=200)
    email = models.EmailField(max_length=254)
    institution = models.CharField(max_length=200, blank=True, default="")
    has_conflict = models.BooleanField(default=False)
    conflict_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """
        Database table, constraints and indexes for committee members.
        """

        db_table = "defense_member"
        verbose_name = "Membro da banca"
        verbose_name_plural = "Membros da banca"
        ordering = ["role", "name"]
        constraints = [
            # Apenas um presidente por banca.
            models.UniqueConstraint(
                fields=["defense", "role"],
                condition=Q(role="PRESIDENT"),
                name="uniq_president_per_defense",
            ),
            # Exatamente uma identidade por membro: institucional XOR externo.
            models.CheckConstraint(
                condition=(
                    Q(institutional_member__isnull=True, external_invitation__isnull=False)
                    | Q(institutional_member__isnull=False, external_invitation__isnull=True)
                ),
                name="defense_member_one_identity",
            ),
        ]
        indexes = [
            models.Index(fields=["defense", "role"], name="defense_member_role_idx"),
        ]

    def __str__(self) -> str:
        """
        Return the member name and role.

        Returns:
            str: Name and role code.
        """
        return f"{self.name} ({self.role})"


class DefenseMemberEvaluation(models.Model):
    """
    Individual evaluation submitted by a non-alternate committee member.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.OneToOneField(
        DefenseMember,
        on_delete=models.CASCADE,
        related_name="evaluation",
    )
    decision = models.CharField(max_length=30, choices=DefenseResult.choices)
    score = models.DecimalField(max_digits=4, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True, default="")
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """
        Database table, constraints and indexes for committee evaluations.
        """

        db_table = "defense_member_evaluation"
        verbose_name = "Avaliação da banca"
        verbose_name_plural = "Avaliações da banca"
        ordering = ["-submitted_at"]
        constraints = [
            # Nota deve estar entre 0,00 e 10,00, se informada.
            models.CheckConstraint(
                condition=Q(score__isnull=True) | (Q(score__gte=0) & Q(score__lte=10)),
                name="defense_evaluation_score_range",
            ),
        ]
        indexes = [
            models.Index(fields=["member"], name="defense_evaluation_member_idx"),
        ]

    def __str__(self) -> str:
        """
        Return the member and decision identifiers.

        Returns:
            str: Member identifier and decision code.
        """
        return f"{self.member_id}:{self.decision}"
