"""
Domain services for thesis submission, advisor review and publication.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Max, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from academic.models import Course, UserCourseAssignment
from academic.services import active_course_assignment
from accounts.concurrency import THESIS_DRAFT_LOCK_KEY, acquire_transaction_lock
from accounts.models import AuditAction, ProfileRole, User
from accounts.network import get_client_ip
from accounts.services import active_role_codes, record_audit
from theses.events import record_thesis_defense_result_recorded, record_thesis_published, record_thesis_submitted
from theses.models import (
    APPROVED_DEFENSE_STATUSES,
    DEFENSE_RESULT_STATUSES,
    EDITABLE_STATUSES,
    PUBLISHABLE_STATUSES,
    REVIEWABLE_STATUSES,
    DefenseResult,
    DocumentStatus,
    DocumentType,
    Keyword,
    ProcessingStatus,
    ReviewDecision,
    StoredFile,
    Thesis,
    ThesisAuthor,
    ThesisDocument,
    ThesisFile,
    ThesisKeyword,
    ThesisReview,
    ThesisStatus,
    ThesisStatusHistory,
    ThesisType,
)

logger = logging.getLogger(__name__)

PDF_MAGIC_BYTES = b"%PDF-"
STATUS_DESCRIPTIONS: dict[str, str] = {
    ThesisStatus.DRAFT: "Preencha os dados da monografia e envie o PDF para submeter.",
    ThesisStatus.SUBMITTED: "Submissão recebida. Aguardando a análise do orientador.",
    ThesisStatus.ADVISOR_REVIEW: "O orientador está analisando a versão enviada.",
    ThesisStatus.CHANGES_REQUESTED: "O orientador solicitou ajustes. Envie uma nova versão.",
    ThesisStatus.ADVISOR_APPROVED: "Aprovada pelo orientador. Aguardando o agendamento da banca.",
    ThesisStatus.READY_FOR_DEFENSE: "Monografia pronta. Aguardando o agendamento da banca.",
    ThesisStatus.DEFENSE_SCHEDULED: "A banca foi agendada. Acompanhe as próximas etapas.",
    ThesisStatus.UNDER_EVALUATION: "A monografia está em avaliação pela banca.",
    ThesisStatus.APPROVED: "Aprovada pela banca. Aguardando a publicação no repositório.",
    ThesisStatus.APPROVED_WITH_REMARKS: "Aprovada com ressalvas. Aguardando correções e publicação.",
    ThesisStatus.REJECTED: "A submissão foi reprovada. Revise o trabalho e envie uma nova versão.",
    ThesisStatus.CORRECTIONS_PENDING: "Correções pendentes após a avaliação da banca.",
    ThesisStatus.PUBLISHED: "Monografia publicada no repositório institucional.",
    ThesisStatus.ARCHIVED: "Monografia arquivada.",
}
WORKFLOW_STEPS: dict[str, int] = {
    ThesisStatus.DRAFT: 1,
    ThesisStatus.SUBMITTED: 2,
    ThesisStatus.ADVISOR_REVIEW: 2,
    ThesisStatus.CHANGES_REQUESTED: 1,
    ThesisStatus.REJECTED: 1,
    ThesisStatus.ADVISOR_APPROVED: 3,
    ThesisStatus.READY_FOR_DEFENSE: 3,
    ThesisStatus.DEFENSE_SCHEDULED: 4,
    ThesisStatus.UNDER_EVALUATION: 4,
    ThesisStatus.APPROVED: 5,
    ThesisStatus.APPROVED_WITH_REMARKS: 5,
    ThesisStatus.CORRECTIONS_PENDING: 5,
    ThesisStatus.PUBLISHED: 5,
    ThesisStatus.ARCHIVED: 5,
}


def user_roles(user: User) -> list[str]:
    """
    Return the active role codes for a user.

    Args:
        user: Institutional user.

    Returns:
        list[str]: Active role codes ordered by business priority.
    """
    return active_role_codes(user)


def user_is_admin(user: User) -> bool:
    """
    Return whether the user currently has administrative privileges.

    Args:
        user: Institutional user.

    Returns:
        bool: ``True`` for active administrators and superusers.
    """
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return ProfileRole.ADMIN in user_roles(user)


def user_is_coordinator(user: User) -> bool:
    """
    Return whether the user currently coordinates at least one course.

    Args:
        user: Institutional user.

    Returns:
        bool: ``True`` when the coordinator role is active.
    """
    return ProfileRole.COORDINATOR in user_roles(user)


def user_is_advisor(user: User) -> bool:
    """
    Return whether the user currently acts as an advisor.

    Args:
        user: Institutional user.

    Returns:
        bool: ``True`` when the advisor role is active.
    """
    return ProfileRole.ADVISOR in user_roles(user)


def user_is_student(user: User) -> bool:
    """
    Return whether the user currently acts as a student.

    Args:
        user: Institutional user.

    Returns:
        bool: ``True`` when the student role is active.
    """
    return ProfileRole.STUDENT in user_roles(user)


def available_advisors() -> QuerySet[User]:
    """
    Return active users eligible to advise theses.

    Returns:
        QuerySet[User]: Active advisors ordered by name.
    """
    return User.objects.filter(
        is_active=True,
        profile_roles__role=ProfileRole.ADVISOR,
        profile_roles__revoked_at__isnull=True,
    ).distinct().order_by("name", "email")


def available_courses_for_student(user: User) -> list[Course]:
    """
    Return the courses a student may select for a thesis.

    Args:
        user: Student user.

    Returns:
        list[Course]: Active assignment course when present, otherwise all courses.
    """
    assignment = active_course_assignment(user)
    if assignment is not None:
        return [assignment.course]
    return list(Course.objects.select_related("department").order_by("name"))


def user_can_access_thesis(user: User, thesis: Thesis) -> bool:
    """
    Return whether a user may read a thesis and its files.

    Args:
        user: Institutional user.
        thesis: Thesis instance.

    Returns:
        bool: ``True`` when the user is an author, advisor, course coordinator or admin.
    """
    if user_is_admin(user):
        return True
    roles = user_roles(user)
    if ProfileRole.ADVISOR in roles and thesis.advisor_id == user.pk:
        return True
    if ProfileRole.STUDENT in roles:
        if thesis.authors.filter(author_id=user.pk).exists():
            return True
    if ProfileRole.COORDINATOR in roles:
        course = thesis.course
        if course.coordinator_id == user.pk:
            return True
        if course.coordinator_email and course.coordinator_email.lower() == user.email.lower():
            return True
        if UserCourseAssignment.objects.filter(
            user=user,
            course=course,
            is_active=True,
            revoked_at__isnull=True,
        ).exists():
            return True
    return False


# TODO(auditoria): membro institucional de banca precisa ler a monografia que
# avalia (detalhe e download do PDF), mas hoje `theses_visible_to` e
# `user_can_access_thesis` não incluem o membro da comissão. Ao adicionar essa
# leitura, NÃO reutilizar escopos de gestão (`coordinator_managed_theses`,
# `_can_manage_defense`): a leitura deve ser somente para detalhe/download, e as
# escritas (upload, parecer, agendamento, membros) continuam exigindo
# `user_can_access_thesis`/regras de gestão. Coordenador-membro de outro curso
# não pode gerenciar a banca por conta da leitura.
def theses_visible_to(user: User, base_queryset: QuerySet[Thesis] | None = None) -> QuerySet[Thesis]:
    """
    Return the thesis queryset visible to a user according to their roles.

    Args:
        user: Institutional user.
        base_queryset: Optional queryset with related objects already configured.
            Visibilities are applied on top of it, preserving its prefetches.

    Returns:
        QuerySet[Thesis]: Role-scoped queryset.
    """
    queryset = base_queryset if base_queryset is not None else Thesis.objects.filter(deleted_at__isnull=True)
    if user_is_admin(user):
        return queryset
    roles = user_roles(user)
    visibility = Q(pk__in=[])
    if ProfileRole.ADVISOR in roles:
        visibility |= Q(advisor_id=user.pk)
    if ProfileRole.STUDENT in roles:
        visibility |= Q(authors__author_id=user.pk)
    if ProfileRole.COORDINATOR in roles:
        visibility |= coordinated_courses_filter(user, prefix="course__")
    return queryset.filter(visibility).distinct()


def coordinated_courses_filter(user: User, prefix: str = "") -> Q:
    """
    Build the filter matching the courses a user coordinates.

    A course counts as coordinated through its ``coordinator`` FK, its
    ``coordinator_email`` (case-insensitive, for coordinators registered before
    their first login) or an active ``UserCourseAssignment``. Callers must check
    the COORDINATOR role themselves; the filter only encodes the course link.

    Args:
        user: Institutional user.
        prefix: Lookup path from the queried model to ``Course`` (for example
            ``"course__"`` for theses, ``""`` for courses).

    Returns:
        Q: Filter combining the three course-link rules.
    """
    by_coordinator = Q(**{f"{prefix}coordinator_id": user.pk})
    by_email = Q(**{f"{prefix}coordinator_email__iexact": user.email})
    by_assignment = Q(
        **{
            f"{prefix}user_assignments__user_id": user.pk,
            f"{prefix}user_assignments__is_active": True,
            f"{prefix}user_assignments__revoked_at__isnull": True,
        }
    )
    return by_coordinator | by_email | by_assignment


def thesis_list_queryset() -> QuerySet[Thesis]:
    """
    Return the base queryset used by thesis lists and dashboards.

    Only the relations read by ``thesis_list_payload`` are loaded, so listing pages
    avoid the N+1 queries caused by accessing authors, keywords and the current
    file per thesis.

    Returns:
        QuerySet[Thesis]: Query with list dependencies prefetched.
    """
    return Thesis.objects.select_related(
        "course",
        "course__department",
        "advisor",
    ).prefetch_related(
        "authors__author",
        "keyword_links__keyword",
        "files__stored_file",
    )


def thesis_queryset() -> QuerySet[Thesis]:
    """
    Return the base queryset used by the thesis API.

    Returns:
        QuerySet[Thesis]: Query with related objects prefetched for serialization.
    """
    return thesis_list_queryset().prefetch_related(
        "files__reviews__reviewer",
        "status_history__changed_by",
        "documents__stored_file",
        "documents__submitted_by",
        "documents__validated_by",
    )


def get_accessible_thesis(user: User, thesis_id: str) -> Thesis:
    """
    Load a thesis visible to the user or raise a validation error.

    Args:
        user: Institutional user.
        thesis_id: Thesis identifier.

    Returns:
        Thesis: Loaded thesis.

    Raises:
        ValidationError: When the thesis does not exist or is not visible.
    """
    thesis = theses_visible_to(user).select_related("course", "advisor").filter(pk=thesis_id).first()
    if thesis is None:
        raise ValidationError("Monografia não encontrada.")
    return thesis


def _ensure_editable(thesis: Thesis) -> None:
    """
    Ensure the thesis can still be edited by the student.

    Args:
        thesis: Thesis instance.

    Raises:
        ValidationError: When the current status is locked for student edits.
    """
    if thesis.status not in EDITABLE_STATUSES:
        raise ValidationError("Esta monografia não pode mais ser editada no status atual.")


def _main_author(thesis: Thesis) -> ThesisAuthor | None:
    """
    Return the main author record for a thesis.

    Args:
        thesis: Thesis instance.

    Returns:
        ThesisAuthor | None: Main author record when present.
    """
    return thesis.authors.filter(is_main_author=True).first()


def _ensure_student_owner(user: User, thesis: Thesis) -> None:
    """
    Ensure the user is the main author of the thesis.

    Args:
        user: Institutional user.
        thesis: Thesis instance.

    Raises:
        ValidationError: When the user is not the main author.
    """
    author = _main_author(thesis)
    if author is None or author.author_id != user.pk:
        raise ValidationError("Apenas o autor principal pode alterar esta monografia.")


def _validate_advisor(advisor: User) -> None:
    """
    Validate that a user can act as an advisor.

    Args:
        advisor: Candidate advisor.

    Raises:
        ValidationError: When the user is not an active advisor.
    """
    if not advisor.is_active or not advisor.has_active_role(ProfileRole.ADVISOR):
        raise ValidationError("Selecione um orientador ativo com o papel de orientador.")


def _validate_course(course: Course) -> None:
    """
    Validate that the course exists and is available for a thesis.

    Args:
        course: Candidate course.

    Raises:
        ValidationError: When the course is not available.
    """
    if course is None:
        raise ValidationError("Selecione um curso válido.")


def normalize_keyword_names(values: Iterable[str]) -> list[str]:
    """
    Normalize, deduplicate and limit keyword names.

    Args:
        values: Raw keyword names.

    Returns:
        list[str]: Clean keyword names preserving input order.

    Raises:
        ValidationError: When too many keywords are provided.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = " ".join((value or "").split())
        if not name:
            continue
        normalized = name.casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(name[:50])
    maximum = getattr(settings, "THESIS_MAX_KEYWORDS", 10)
    if len(cleaned) > maximum:
        raise ValidationError(f"Informe no máximo {maximum} palavras-chave.")
    return cleaned


def _sync_keywords(thesis: Thesis, keyword_names: Iterable[str]) -> None:
    """
    Synchronize the keyword links of a thesis.

    Args:
        thesis: Thesis instance.
        keyword_names: Normalized keyword names.
    """
    keywords: list[Keyword] = []
    for name in normalize_keyword_names(keyword_names):
        normalized = name.casefold()
        try:
            keyword, _ = Keyword.objects.get_or_create(
                normalized_name=normalized, defaults={"name": name}
            )
        except IntegrityError:
            keyword = Keyword.objects.get(normalized_name=normalized)
        keywords.append(keyword)
    existing_links = list(thesis.keyword_links.select_related("keyword"))
    desired_ids = {keyword.pk for keyword in keywords}
    for link in existing_links:
        if link.keyword_id not in desired_ids:
            link.delete()
    existing_ids = {link.keyword_id for link in existing_links if link.keyword_id in desired_ids}
    for keyword in keywords:
        if keyword.pk not in existing_ids:
            ThesisKeyword.objects.create(thesis=thesis, keyword=keyword)


def _add_status_history(thesis: Thesis, previous_status: str, new_status: str, actor: User | None, reason: str = "", metadata: dict | None = None) -> ThesisStatusHistory:
    """
    Append a status transition to the thesis history.

    Args:
        thesis: Thesis instance.
        previous_status: Status before the transition.
        new_status: Status after the transition.
        actor: User responsible for the change.
        reason: Optional human-readable reason.
        metadata: Optional structured metadata.

    Returns:
        ThesisStatusHistory: Created history entry.
    """
    return ThesisStatusHistory.objects.create(
        thesis=thesis,
        previous_status=previous_status or "",
        new_status=new_status,
        changed_by=actor,
        reason=reason,
        metadata=metadata or {},
    )


def _audit_thesis(action: str, thesis: Thesis, request: HttpRequest | None, actor: User | None, metadata: dict | None = None, changes: dict | None = None) -> None:
    """
    Record an audit entry for a thesis action.

    The thesis title, reference code, course, advisor and author names are added
    to the metadata so old entries stay readable even after the thesis is no
    longer available for lookup.

    Args:
        action: Audit action code.
        thesis: Thesis instance.
        request: HTTP request used for context.
        actor: User responsible for the action.
        metadata: Optional non-sensitive metadata.
        changes: Optional field changes.
    """
    enriched_metadata: dict[str, object] = {
        "thesisTitle": thesis.title,
        "thesisReference": thesis.reference_code,
        "courseId": str(thesis.course_id),
        "advisorId": str(thesis.advisor_id),
    }
    author_names = [
        link.author.name or link.author.email
        for link in thesis.authors.select_related("author").order_by("position")
    ]
    if author_names:
        enriched_metadata["thesisAuthorNames"] = author_names
    enriched_metadata.update(metadata or {})
    record_audit(
        action=action,
        entity_type="Thesis",
        entity_id=str(thesis.pk),
        entity_app_label="theses",
        request=request,
        actor_user=actor,
        metadata=enriched_metadata,
        changes=changes or {},
    )


@transaction.atomic
def save_thesis_metadata(user: User, course: Course, advisor: User, title: str, abstract: str, keyword_names: Iterable[str], thesis_type: str = ThesisType.MONOGRAPH, journal: str = "", request: HttpRequest | None = None) -> Thesis:
    """
    Create or update the thesis metadata while it is editable.

    Args:
        user: Student author.
        course: Selected course.
        advisor: Selected advisor.
        title: Thesis title.
        abstract: Thesis abstract.
        keyword_names: Keyword names.
        thesis_type: Thesis type code.
        journal: Optional journal name for articles.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Created or updated thesis.

    Raises:
        ValidationError: When the user is not allowed to edit or data is invalid.
    """
    # Um SELECT FOR UPDATE não PROTEGE linhas que ainda não existem: sem esse
    # lock, dois PUT concorrentes criam dois rascunhos para o mesmo autor.
    acquire_transaction_lock(THESIS_DRAFT_LOCK_KEY + (user.pk.int % 100000))
    _validate_course(course)
    _validate_advisor(advisor)
    clean_title = " ".join((title or "").split())
    if not clean_title:
        raise ValidationError("Informe o título da monografia.")
    clean_abstract = (abstract or "").strip()
    if not clean_abstract:
        raise ValidationError("Informe o resumo da monografia.")

    thesis = Thesis.objects.select_for_update().filter(authors__author=user, authors__is_main_author=True).first()
    created = thesis is None
    if thesis is not None:
        _ensure_student_owner(user, thesis)
        _ensure_editable(thesis)
        thesis.course = course
        thesis.advisor = advisor
        thesis.title = clean_title
        thesis.abstract = clean_abstract
        thesis.type = thesis_type
        thesis.journal = (journal or "").strip()
        thesis.save()
    else:
        thesis = Thesis.objects.create(
            course=course,
            advisor=advisor,
            title=clean_title,
            abstract=clean_abstract,
            type=thesis_type,
            journal=(journal or "").strip(),
            status=ThesisStatus.DRAFT,
        )
        ThesisAuthor.objects.create(
            thesis=thesis,
            author=user,
            position=1,
            is_main_author=True,
        )
        _add_status_history(thesis, "", ThesisStatus.DRAFT, user, "Rascunho criado.")

    _sync_keywords(thesis, keyword_names)
    action = AuditAction.CREATE if created else AuditAction.UPDATE
    _audit_thesis(
        action,
        thesis,
        request,
        user,
        metadata={"title": thesis.title, "courseId": str(thesis.course_id), "advisorId": str(thesis.advisor_id)},
    )
    return thesis


def _max_bytes(setting_name: str, default_mb: int) -> int:
    """
    Resolve an upload size limit in bytes.

    Args:
        setting_name: Django setting name containing the size in megabytes.
        default_mb: Fallback size in megabytes.

    Returns:
        int: Limit in bytes.
    """
    megabytes = getattr(settings, setting_name, default_mb)
    return int(megabytes) * 1024 * 1024


def _validate_pdf_upload(uploaded_file: Any, setting_name: str, default_mb: int) -> tuple[bytes, int]:
    """
    Validate a PDF upload and calculate its SHA-256 digest.

    Args:
        uploaded_file: Uploaded file object.
        setting_name: Setting name with the maximum size in megabytes.
        default_mb: Fallback maximum size in megabytes.

    Returns:
        tuple[bytes, int]: Digest bytes and file size.

    Raises:
        ValidationError: When the file is empty, too large or not a PDF.
    """
    if uploaded_file is None:
        raise ValidationError("Selecione um arquivo PDF.")
    maximum_bytes = _max_bytes(setting_name, default_mb)
    reported_size = getattr(uploaded_file, "size", None)
    if reported_size is not None and reported_size > maximum_bytes:
        limit_mb = maximum_bytes // (1024 * 1024)
        raise ValidationError(f"O arquivo excede o limite de {limit_mb} MB.")
    header = uploaded_file.read(5)
    uploaded_file.seek(0)
    if header != PDF_MAGIC_BYTES:
        raise ValidationError("Envie um arquivo PDF válido.")
    digest = hashlib.sha256()
    size = 0
    for chunk in uploaded_file.chunks():
        size += len(chunk)
        if size > maximum_bytes:
            limit_mb = maximum_bytes // (1024 * 1024)
            raise ValidationError(f"O arquivo excede o limite de {limit_mb} MB.")
        digest.update(chunk)
    uploaded_file.seek(0)
    if size == 0:
        raise ValidationError("O arquivo enviado está vazio.")
    return digest.digest(), size


def _store_pdf(uploaded_file: Any, thesis: Thesis, user: User, setting_name: str, default_mb: int, prefix: str = "theses") -> StoredFile:
    """
    Validate and store a PDF, reusing an existing file with the same digest.

    Args:
        uploaded_file: Uploaded file object.
        thesis: Thesis that owns the upload.
        user: User performing the upload.
        setting_name: Setting name with the maximum size in megabytes.
        default_mb: Fallback maximum size in megabytes.
        prefix: Storage directory prefix.

    Returns:
        StoredFile: Persisted stored file.

    Raises:
        ValidationError: When the upload is invalid.
    """
    digest, size = _validate_pdf_upload(uploaded_file, setting_name, default_mb)
    existing = StoredFile.objects.filter(sha256=digest).first()
    if existing is not None and default_storage.exists(existing.storage_path):
        return existing

    safe_name = f"{prefix}/{thesis.pk}/{uuid4().hex}.pdf"
    stored_name = default_storage.save(safe_name, uploaded_file)
    original_name = Path(getattr(uploaded_file, "name", "monografia.pdf")).name[:255]
    stored_file = StoredFile.objects.create(
        original_filename=original_name or "monografia.pdf",
        stored_filename=Path(stored_name).name,
        mime_type="application/pdf",
        extension=".pdf",
        size_bytes=size,
        sha256=digest,
        storage_path=stored_name,
        uploaded_by=user,
        processing_status=ProcessingStatus.PENDING,
    )
    logger.info("Stored PDF file %s for thesis %s", stored_file.pk, thesis.pk)
    return stored_file


def verify_stored_file_integrity(stored_file: StoredFile) -> bool:
    """
    Recalculate the SHA-256 digest of a stored file and compare it with the database.

    Args:
        stored_file: Stored file to verify.

    Returns:
        bool: ``True`` when the file exists and the digest matches.
    """
    if not default_storage.exists(stored_file.storage_path):
        return False
    digest = hashlib.sha256()
    with default_storage.open(stored_file.storage_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.digest() == bytes(stored_file.sha256)


def verify_thesis_file_integrity(thesis_file: ThesisFile) -> bool:
    """
    Verify a thesis version and record the verification timestamp.

    Args:
        thesis_file: Thesis version to verify.

    Returns:
        bool: ``True`` when the stored content matches the recorded digest.
    """
    if not verify_stored_file_integrity(thesis_file.stored_file):
        return False
    thesis_file.hash_verified_at = timezone.now()
    thesis_file.save(update_fields=["hash_verified_at"])
    return True


@transaction.atomic
def upload_thesis_file(user: User, thesis: Thesis, uploaded_file: Any, change_description: str = "", request: HttpRequest | None = None) -> ThesisFile:
    """
    Upload a new PDF version for an editable thesis.

    Args:
        user: Student author performing the upload.
        thesis: Thesis instance.
        uploaded_file: Uploaded PDF file.
        change_description: Optional description of the changes.
        request: HTTP request used for audit context.

    Returns:
        ThesisFile: Created version.

    Raises:
        ValidationError: When the thesis is not editable or the file is invalid.
    """
    thesis = Thesis.objects.select_for_update().get(pk=thesis.pk)
    _ensure_student_owner(user, thesis)
    _ensure_editable(thesis)
    stored_file = _store_pdf(
        uploaded_file,
        thesis,
        user,
        "THESIS_MAX_PDF_SIZE_MB",
        50,
    )
    current = thesis.files.filter(is_current=True).first()
    if current is not None and bytes(current.stored_file.sha256) == bytes(stored_file.sha256):
        raise ValidationError("O arquivo enviado é idêntico à versão atual.")
    next_version = thesis.files.aggregate(max_version=Max("version_number"))["max_version"] or 0
    next_version += 1
    if current is not None:
        current.is_current = False
        current.save(update_fields=["is_current"])
    thesis_file = ThesisFile.objects.create(
        thesis=thesis,
        stored_file=stored_file,
        version_number=next_version,
        is_current=True,
        change_description=(change_description or "").strip(),
        uploaded_by=user,
    )
    thesis.save(update_fields=["updated_at"])
    _audit_thesis(
        AuditAction.UPLOAD,
        thesis,
        request,
        user,
        metadata={
            "versionNumber": next_version,
            "sha256": stored_file.sha256_hex,
            "sizeBytes": stored_file.size_bytes,
        },
    )
    return thesis_file


@transaction.atomic
def submit_thesis(user: User, thesis: Thesis, request: HttpRequest | None = None) -> Thesis:
    """
    Submit the current thesis version for advisor review.

    Args:
        user: Student author.
        thesis: Thesis instance.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Updated thesis.

    Raises:
        ValidationError: When required data or the current file is missing.
    """
    thesis = Thesis.objects.select_for_update().select_related("advisor", "course").get(pk=thesis.pk)
    _ensure_student_owner(user, thesis)
    _ensure_editable(thesis)
    if not thesis.title.strip() or not thesis.abstract.strip():
        raise ValidationError("Preencha o título e o resumo antes de submeter.")
    if not thesis.keyword_links.exists():
        raise ValidationError("Informe pelo menos uma palavra-chave.")
    current = thesis.files.filter(is_current=True).select_related("stored_file").first()
    if current is None:
        raise ValidationError("Envie o arquivo PDF antes de submeter.")
    if not verify_thesis_file_integrity(current):
        raise ValidationError("O arquivo da monografia falhou na verificação de integridade. Envie o PDF novamente.")
    previous_status = thesis.status
    now = timezone.now()
    thesis.status = ThesisStatus.SUBMITTED
    thesis.submitted_at = now
    thesis.save(update_fields=["status", "submitted_at", "updated_at"])
    _add_status_history(thesis, previous_status, ThesisStatus.SUBMITTED, user, "Monografia submetida para avaliação do orientador.")
    record_thesis_submitted(thesis, current.version_number)
    _audit_thesis(
        AuditAction.UPDATE,
        thesis,
        request,
        user,
        metadata={"event": "SUBMITTED", "versionNumber": current.version_number},
    )
    return thesis


@transaction.atomic
def withdraw_submission(user: User, thesis: Thesis, request: HttpRequest | None = None) -> Thesis:
    """
    Descarta o envio atual da monografia enquanto não houver parecer do orientador.

    Args:
        user: Aluno autor principal.
        thesis: Monografia enviada.
        request: Requisição HTTP usada no contexto de auditoria.

    Returns:
        Thesis: Monografia atualizada em estado editável.

    Raises:
        ValidationError: Quando o usuário não pode descartar ou o envio já foi avaliado.
    """
    thesis = Thesis.objects.select_for_update().select_related("advisor", "course").get(pk=thesis.pk)
    _ensure_student_owner(user, thesis)
    if thesis.status != ThesisStatus.SUBMITTED:
        raise ValidationError("Apenas envios ainda não avaliados pelo orientador podem ser descartados.")
    previous_submission = (
        thesis.status_history.filter(new_status=ThesisStatus.SUBMITTED)
        .order_by("-changed_at")
        .first()
    )
    target_status = ThesisStatus.DRAFT
    if previous_submission and previous_submission.previous_status in EDITABLE_STATUSES:
        target_status = previous_submission.previous_status
    previous_status = thesis.status
    thesis.status = target_status
    thesis.submitted_at = None if target_status == ThesisStatus.DRAFT else thesis.submitted_at
    thesis.save(update_fields=["status", "submitted_at", "updated_at"])
    _add_status_history(
        thesis,
        previous_status,
        target_status,
        user,
        "Envio descartado pelo aluno antes da avaliação do orientador.",
        metadata={"event": "SUBMISSION_WITHDRAWN"},
    )
    _audit_thesis(
        AuditAction.UPDATE,
        thesis,
        request,
        user,
        metadata={"event": "SUBMISSION_WITHDRAWN", "targetStatus": target_status},
    )
    return thesis


@transaction.atomic
def review_thesis(user: User, thesis: Thesis, decision: str, comments: str = "", recommendations: str = "", request: HttpRequest | None = None) -> ThesisReview:
    """
    Record an advisor decision for the current thesis version.

    Args:
        user: Advisor responsible for the review.
        thesis: Thesis instance.
        decision: Review decision code.
        comments: Feedback sent to the student.
        recommendations: Optional recommendations.
        request: HTTP request used for audit context.

    Returns:
        ThesisReview: Created review.

    Raises:
        ValidationError: When the user cannot review or the transition is invalid.
    """
    thesis = Thesis.objects.select_for_update().select_related("advisor", "course").get(pk=thesis.pk)
    if thesis.advisor_id != user.pk:
        raise ValidationError("Apenas o orientador da monografia pode registrar um parecer.")
    if thesis.status not in REVIEWABLE_STATUSES:
        raise ValidationError("Esta monografia não está aguardando parecer do orientador.")
    if decision not in ReviewDecision.values:
        raise ValidationError("Decisão de parecer inválida.")
    clean_comments = (comments or "").strip()
    if decision in {ReviewDecision.CHANGES_REQUESTED, ReviewDecision.REJECTED} and not clean_comments:
        raise ValidationError("Descreva os ajustes ou o motivo da reprovação.")
    current = thesis.files.filter(is_current=True).select_related("stored_file").first()
    if current is None:
        raise ValidationError("A monografia não possui versão atual para avaliação.")
    if not verify_thesis_file_integrity(current):
        raise ValidationError("O arquivo da monografia falhou na verificação de integridade. Solicite um novo envio ao aluno.")

    previous_status = thesis.status
    if decision == ReviewDecision.APPROVED:
        new_status = ThesisStatus.ADVISOR_APPROVED
        thesis.advisor_approved_at = timezone.now()
        audit_action = AuditAction.APPROVE
    elif decision == ReviewDecision.CHANGES_REQUESTED:
        new_status = ThesisStatus.CHANGES_REQUESTED
        audit_action = AuditAction.REVIEW
    else:
        new_status = ThesisStatus.REJECTED
        audit_action = AuditAction.REJECT
    review = ThesisReview.objects.create(
        thesis_file=current,
        reviewer=user,
        decision=decision,
        comments=clean_comments,
        recommendations=(recommendations or "").strip(),
        reviewed_at=timezone.now(),
        ip_address=get_client_ip(request) if request is not None else None,
    )
    thesis.status = new_status
    thesis.save(update_fields=["status", "advisor_approved_at", "updated_at"])
    _add_status_history(
        thesis,
        previous_status,
        new_status,
        user,
        clean_comments,
        metadata={"decision": decision, "versionNumber": current.version_number},
    )
    _audit_thesis(
        audit_action,
        thesis,
        request,
        user,
        metadata={"decision": decision, "versionNumber": current.version_number, "reviewId": str(review.pk)},
    )
    return review


def _delete_stored_paths(paths: Iterable[str]) -> None:
    """Remove arquivos físicos órfãos após o commit da transação."""
    for path in paths:
        try:
            if default_storage.exists(path):
                default_storage.delete(path)
        except OSError:
            logger.warning("Falha ao remover arquivo armazenado: path=%s", path)


@transaction.atomic
def purge_thesis_versions(thesis: Thesis, actor: User | None = None, request: HttpRequest | None = None) -> int:
    """
    Delete older thesis versions after final defense approval.

    The current version is always preserved. Reviews linked to deleted versions are
    removed together with the version; the append-only audit log remains intact.

    Args:
        thesis: Thesis instance.
        actor: User responsible for the purge.
        request: HTTP request used for audit context.

    Returns:
        int: Number of removed versions.

    Raises:
        ValidationError: When the thesis has no current version.
    """
    thesis = Thesis.objects.select_for_update().get(pk=thesis.pk)
    current = thesis.files.filter(is_current=True).first()
    if current is None:
        raise ValidationError("A monografia não possui versão atual.")
    old_files = list(thesis.files.exclude(pk=current.pk).select_related("stored_file"))
    removed = 0
    paths_to_delete: list[str] = []
    for thesis_file in old_files:
        stored_file_id = thesis_file.stored_file_id
        thesis_file.delete()
        removed += 1
        still_referenced = ThesisFile.objects.filter(stored_file_id=stored_file_id).exists()
        if not still_referenced:
            still_referenced = ThesisDocument.objects.filter(stored_file_id=stored_file_id).exists()
        if still_referenced:
            continue
        stored_file = StoredFile.objects.filter(pk=stored_file_id).first()
        if stored_file is None:
            continue
        paths_to_delete.append(stored_file.storage_path)
        stored_file.delete()
    # Storage não participa da transação: excluir o PDF antes do commit pode
    # apagar o arquivo de uma operação que depois é revertida.
    transaction.on_commit(lambda paths=paths_to_delete: _delete_stored_paths(paths))
    if removed:
        _audit_thesis(
            AuditAction.DELETE,
            thesis,
            request,
            actor,
            metadata={"event": "VERSION_HISTORY_PURGED", "removedVersions": removed, "keptVersion": current.version_number},
        )
    return removed


@transaction.atomic
def record_defense_result(user: User, thesis: Thesis, result: str, reason: str = "", request: HttpRequest | None = None) -> Thesis:
    """
    Record the final defense result and purge older versions when approved.

    Args:
        user: Coordinator or administrator recording the result.
        thesis: Thesis instance.
        result: Defense result code.
        reason: Optional justification.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Updated thesis.

    Raises:
        ValidationError: When the user or transition is not allowed.
    """
    if not (user_is_admin(user) or user_is_coordinator(user)):
        raise ValidationError("Apenas coordenadores ou administradores podem registrar o resultado da banca.")
    thesis = Thesis.objects.select_for_update().select_related("course", "advisor").get(pk=thesis.pk)
    if thesis.status not in DEFENSE_RESULT_STATUSES:
        raise ValidationError("Esta monografia não está em etapa de resultado da banca.")
    if result not in DefenseResult.values:
        raise ValidationError("Resultado da banca inválido.")
    previous_status = thesis.status
    if result == DefenseResult.APPROVED:
        new_status = ThesisStatus.APPROVED
        audit_action = AuditAction.APPROVE
    elif result == DefenseResult.APPROVED_WITH_REMARKS:
        new_status = ThesisStatus.APPROVED_WITH_REMARKS
        audit_action = AuditAction.APPROVE
    else:
        new_status = ThesisStatus.REJECTED
        audit_action = AuditAction.REJECT
    thesis.status = new_status
    thesis.save(update_fields=["status", "updated_at"])
    _add_status_history(thesis, previous_status, new_status, user, reason, metadata={"defenseResult": result})
    record_thesis_defense_result_recorded(thesis, result, new_status)
    if new_status in APPROVED_DEFENSE_STATUSES:
        purge_thesis_versions(thesis, actor=user, request=request)
    _audit_thesis(
        audit_action,
        thesis,
        request,
        user,
        metadata={"event": "DEFENSE_RESULT", "result": result},
    )
    return thesis


@transaction.atomic
# TODO(auditoria): ao implementar cache do repositório público, invalidar aqui
# (o ponto natural é logo após salvar `Publicado`); não usar delete amplo por
# prefixo, mas girar uma versão de chaves para invalidar páginas da busca.
def publish_thesis(user: User, thesis: Thesis, request: HttpRequest | None = None) -> Thesis:
    """
    Publish an approved thesis in the public repository.

    Args:
        user: Coordinator or administrator performing the publication.
        thesis: Thesis instance.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Published thesis.

    Raises:
        ValidationError: When eligibility or integrity requirements are not met.
    """
    if not (user_is_admin(user) or user_is_coordinator(user)):
        raise ValidationError("Apenas coordenadores ou administradores podem publicar monografias.")
    thesis = Thesis.objects.select_for_update().select_related("course", "advisor").get(pk=thesis.pk)
    if thesis.status not in PUBLISHABLE_STATUSES:
        raise ValidationError("A monografia precisa estar aprovada pela banca para ser publicada.")
    current = thesis.files.filter(is_current=True).select_related("stored_file").first()
    if current is None:
        raise ValidationError("A monografia não possui versão atual.")
    if not verify_thesis_file_integrity(current):
        raise ValidationError("O arquivo da monografia falhou na verificação de integridade.")
    authorization = thesis.documents.filter(
        type=DocumentType.PUBLICATION_AUTHORIZATION,
        status=DocumentStatus.VALIDATED,
    ).first()
    if authorization is None:
        raise ValidationError("Registre e valide a autorização de publicação antes de publicar.")
    previous_status = thesis.status
    thesis.status = ThesisStatus.PUBLISHED
    thesis.is_public = True
    thesis.published_at = timezone.now()
    thesis.save(update_fields=["status", "is_public", "published_at", "updated_at"])
    _add_status_history(thesis, previous_status, ThesisStatus.PUBLISHED, user, "Monografia publicada no repositório institucional.")
    record_thesis_published(thesis)
    purge_thesis_versions(thesis, actor=user, request=request)
    _audit_thesis(
        AuditAction.PUBLISH,
        thesis,
        request,
        user,
        metadata={"event": "PUBLISHED", "referenceCode": thesis.reference_code},
    )
    return thesis


@transaction.atomic
def upload_thesis_document(user: User, thesis: Thesis, document_type: str, uploaded_file: Any, request: HttpRequest | None = None) -> ThesisDocument:
    """
    Upload a supplementary PDF document for a thesis.

    Args:
        user: User uploading the document.
        thesis: Thesis instance.
        document_type: Document type code.
        uploaded_file: Uploaded PDF file.
        request: HTTP request used for audit context.

    Returns:
        ThesisDocument: Created document.

    Raises:
        ValidationError: When the user, type or file is invalid.
    """
    if document_type not in DocumentType.values:
        raise ValidationError("Tipo de documento inválido.")
    thesis = Thesis.objects.select_for_update().get(pk=thesis.pk)
    if not user_can_access_thesis(user, thesis):
        raise ValidationError("Você não tem permissão para anexar documentos a esta monografia.")
    if thesis.status in {ThesisStatus.PUBLISHED, ThesisStatus.ARCHIVED}:
        raise ValidationError("Monografias publicadas ou arquivadas não aceitam novos documentos.")
    stored_file = _store_pdf(
        uploaded_file,
        thesis,
        user,
        "THESIS_MAX_DOCUMENT_SIZE_MB",
        20,
        prefix="thesis-documents",
    )
    current = thesis.files.filter(is_current=True).first()
    document = ThesisDocument.objects.create(
        thesis=thesis,
        thesis_file=current,
        stored_file=stored_file,
        type=document_type,
        status=DocumentStatus.SUBMITTED,
        submitted_by=user,
    )
    _audit_thesis(
        AuditAction.UPLOAD,
        thesis,
        request,
        user,
        metadata={"documentType": document_type, "documentId": str(document.pk)},
    )
    return document


@transaction.atomic
def validate_thesis_document(user: User, thesis: Thesis, document: ThesisDocument, request: HttpRequest | None = None) -> ThesisDocument:
    """
    Validate a supplementary thesis document.

    Args:
        user: Coordinator or administrator validating the document.
        thesis: Thesis instance.
        document: Document to validate.
        request: HTTP request used for audit context.

    Returns:
        ThesisDocument: Validated document.

    Raises:
        ValidationError: When the user or document state is invalid.
    """
    if not (user_is_admin(user) or user_is_coordinator(user)):
        raise ValidationError("Apenas coordenadores ou administradores podem validar documentos.")
    document = ThesisDocument.objects.select_for_update().get(pk=document.pk)
    if document.thesis_id != thesis.pk:
        raise ValidationError("Documento não pertence a esta monografia.")
    if document.status == DocumentStatus.VALIDATED:
        return document
    document.status = DocumentStatus.VALIDATED
    document.validated_by = user
    document.validated_at = timezone.now()
    document.rejection_reason = ""
    document.save(update_fields=["status", "validated_by", "validated_at", "rejection_reason"])
    _audit_thesis(
        AuditAction.APPROVE,
        thesis,
        request,
        user,
        metadata={"documentType": document.type, "documentId": str(document.pk), "event": "DOCUMENT_VALIDATED"},
    )
    return document


def user_brief(user: User | None) -> dict[str, Any] | None:
    """
    Serialize a user reference used by thesis payloads.

    Args:
        user: User instance or ``None``.

    Returns:
        dict[str, Any] | None: User summary.
    """
    if user is None:
        return None
    return {
        "id": str(user.pk),
        "name": user.name or user.email,
        "email": user.email,
    }


def course_brief(course: Course) -> dict[str, Any]:
    """
    Serialize a course reference used by thesis payloads.

    Args:
        course: Course instance.

    Returns:
        dict[str, Any]: Course summary.
    """
    return {
        "id": str(course.pk),
        "name": course.name,
        "code": course.code,
    }


def stored_file_payload(stored_file: StoredFile) -> dict[str, Any]:
    """
    Serialize a stored file without exposing internal storage paths.

    Args:
        stored_file: Stored file instance.

    Returns:
        dict[str, Any]: Public file metadata.
    """
    return {
        "id": str(stored_file.pk),
        "originalFilename": stored_file.original_filename,
        "mimeType": stored_file.mime_type,
        "extension": stored_file.extension,
        "sizeBytes": stored_file.size_bytes,
        "sha256": stored_file.sha256_hex,
        "processingStatus": stored_file.processing_status,
        "uploadedAt": stored_file.uploaded_at.isoformat(),
    }


def thesis_file_payload(thesis_file: ThesisFile) -> dict[str, Any]:
    """
    Serialize a thesis version.

    Args:
        thesis_file: Thesis version.

    Returns:
        dict[str, Any]: Version payload including download paths.
    """
    return {
        "id": str(thesis_file.pk),
        "versionNumber": thesis_file.version_number,
        "isCurrent": thesis_file.is_current,
        "changeDescription": thesis_file.change_description,
        "uploadedAt": thesis_file.uploaded_at.isoformat(),
        "hashVerifiedAt": thesis_file.hash_verified_at.isoformat() if thesis_file.hash_verified_at else None,
        "storedFile": stored_file_payload(thesis_file.stored_file),
        "downloadUrl": f"/api/v1/theses/{thesis_file.thesis_id}/files/{thesis_file.pk}/download?disposition=attachment",
        "previewUrl": f"/api/v1/theses/{thesis_file.thesis_id}/files/{thesis_file.pk}/download?disposition=inline",
    }


def review_payload(review: ThesisReview) -> dict[str, Any]:
    """
    Serialize an advisor review.

    Args:
        review: Review instance.

    Returns:
        dict[str, Any]: Review payload.
    """
    return {
        "id": str(review.pk),
        "fileId": str(review.thesis_file_id),
        "decision": review.decision,
        "decisionLabel": ReviewDecision(review.decision).label,
        "comments": review.comments,
        "recommendations": review.recommendations,
        "reviewedAt": review.reviewed_at.isoformat(),
        "reviewer": user_brief(review.reviewer),
    }


def status_history_payload(entry: ThesisStatusHistory) -> dict[str, Any]:
    """
    Serialize a status transition entry.

    Args:
        entry: Status history entry.

    Returns:
        dict[str, Any]: History payload.
    """
    return {
        "id": str(entry.pk),
        "previousStatus": entry.previous_status or None,
        "newStatus": entry.new_status,
        "newStatusLabel": ThesisStatus(entry.new_status).label,
        "reason": entry.reason,
        "metadata": entry.metadata,
        "changedAt": entry.changed_at.isoformat(),
        "changedBy": user_brief(entry.changed_by),
    }


def document_payload(document: ThesisDocument) -> dict[str, Any]:
    """
    Serialize a supplementary thesis document.

    Args:
        document: Document instance.

    Returns:
        dict[str, Any]: Document payload.
    """
    return {
        "id": str(document.pk),
        "type": document.type,
        "typeLabel": DocumentType(document.type).label,
        "status": document.status,
        "statusLabel": DocumentStatus(document.status).label,
        "submittedAt": document.submitted_at.isoformat(),
        "validatedAt": document.validated_at.isoformat() if document.validated_at else None,
        "rejectionReason": document.rejection_reason,
        "submittedBy": user_brief(document.submitted_by),
        "validatedBy": user_brief(document.validated_by),
        "storedFile": stored_file_payload(document.stored_file),
        "downloadUrl": f"/api/v1/theses/{document.thesis_id}/documents/{document.pk}/download",
    }


def _prefetched_main_author(thesis: Thesis) -> User | None:
    """
    Return the main author using the prefetched ``authors`` relation when available.

    Args:
        thesis: Thesis instance (``authors__author`` ideally prefetched).

    Returns:
        User | None: Main author, if any.
    """
    for author_link in thesis.authors.all():
        if author_link.is_main_author:
            return author_link.author
    return None


def _prefetched_current_file(thesis: Thesis) -> ThesisFile | None:
    """
    Return the current file using the prefetched ``files`` relation when available.

    Args:
        thesis: Thesis instance (``files__stored_file`` ideally prefetched).

    Returns:
        ThesisFile | None: Current version, if any.
    """
    for thesis_file in thesis.files.all():
        if thesis_file.is_current:
            return thesis_file
    return None


def _prefetched_keywords(thesis: Thesis) -> list[str]:
    """
    Return keyword names using the prefetched ``keyword_links`` relation when available.

    Args:
        thesis: Thesis instance (``keyword_links__keyword`` ideally prefetched).

    Returns:
        list[str]: Keyword names in link order.
    """
    return [link.keyword.name for link in thesis.keyword_links.all()]


def thesis_summary_payload(thesis: Thesis, include_file_urls: bool = True) -> dict[str, Any]:
    """
    Serialize a compact thesis reference for cross-feature navigation.

    Used by defense payloads so screens can show which monography is being defended
    and link to its PDF without a second request.

    Args:
        thesis: Thesis instance with ``course``, ``advisor``, ``authors`` and ``files``
            related objects available (prefetched when serializing lists).
        include_file_urls: Whether to expose institutional preview/download URLs.
            External committee sessions receive the metadata without direct links,
            because the institutional download endpoints require a real user.

    Returns:
        dict[str, Any]: Thesis summary including student, advisor, course and current file.
    """
    main_author = _prefetched_main_author(thesis)
    current_file = _prefetched_current_file(thesis)
    file_payload: dict[str, Any] | None = None
    if current_file is not None:
        file_payload = {
            "id": str(current_file.pk),
            "versionNumber": current_file.version_number,
            "originalFilename": current_file.stored_file.original_filename,
            "sizeBytes": current_file.stored_file.size_bytes,
            "sha256": current_file.stored_file.sha256_hex,
            "uploadedAt": current_file.uploaded_at.isoformat(),
            "previewUrl": f"/api/v1/theses/{thesis.pk}/files/{current_file.pk}/download?disposition=inline" if include_file_urls else None,
            "downloadUrl": f"/api/v1/theses/{thesis.pk}/files/{current_file.pk}/download?disposition=attachment" if include_file_urls else None,
        }
    return {
        "id": str(thesis.pk),
        "referenceCode": thesis.reference_code,
        "title": thesis.title,
        "type": thesis.type,
        "typeLabel": ThesisType(thesis.type).label,
        "status": thesis.status,
        "statusLabel": ThesisStatus(thesis.status).label,
        "isPublic": thesis.is_public,
        "course": course_brief(thesis.course),
        "advisor": user_brief(thesis.advisor),
        "student": user_brief(main_author),
        "keywords": _prefetched_keywords(thesis),
        "submittedAt": thesis.submitted_at.isoformat() if thesis.submitted_at else None,
        "publishedAt": thesis.published_at.isoformat() if thesis.published_at else None,
        "currentFile": file_payload,
    }


def thesis_permissions(thesis: Thesis, viewer: User | None) -> dict[str, bool]:
    """
    Compute the actions available to a viewer.

    Args:
        thesis: Thesis instance.
        viewer: Authenticated user or ``None`` for public access.

    Returns:
        dict[str, bool]: Action availability flags.
    """
    if viewer is None:
        return {
            "canEdit": False,
            "canUpload": False,
            "canSubmit": False,
            "canWithdrawSubmission": False,
            "canReview": False,
            "canManage": False,
            "canPublish": False,
            "canRecordDefenseResult": False,
            "canUploadDocuments": False,
        }
    roles = user_roles(viewer)
    is_admin = user_is_admin(viewer)
    is_coordinator = ProfileRole.COORDINATOR in roles
    has_advisor_role = ProfileRole.ADVISOR in roles
    is_assigned_advisor = thesis.advisor_id == viewer.pk
    is_advisor = has_advisor_role and is_assigned_advisor
    main_author = _prefetched_main_author(thesis)
    is_author = main_author is not None and main_author.pk == viewer.pk
    editable = thesis.status in EDITABLE_STATUSES
    return {
        "canEdit": bool(is_author and editable),
        "canUpload": bool(is_author and editable),
        "canSubmit": bool(is_author and editable),
        "canWithdrawSubmission": bool(is_author and thesis.status == ThesisStatus.SUBMITTED),
        "canReview": bool(is_advisor and thesis.status in REVIEWABLE_STATUSES),
        "canManage": bool(is_admin or is_coordinator),
        "canPublish": bool((is_admin or is_coordinator) and thesis.status in PUBLISHABLE_STATUSES),
        "canRecordDefenseResult": bool((is_admin or is_coordinator) and thesis.status in DEFENSE_RESULT_STATUSES),
        "canUploadDocuments": bool(user_can_access_thesis(viewer, thesis) and thesis.status not in {ThesisStatus.PUBLISHED, ThesisStatus.ARCHIVED}),
    }


def thesis_payload(thesis: Thesis, viewer: User | None = None, include_history: bool = True) -> dict[str, Any]:
    """
    Serialize a thesis for API responses.

    Args:
        thesis: Thesis instance.
        viewer: Authenticated user or ``None`` for public access.
        include_history: Whether to include versions, reviews and status history.

    Returns:
        dict[str, Any]: Full thesis payload.
    """
    main_author = _prefetched_main_author(thesis)
    keywords = _prefetched_keywords(thesis)
    files = list(thesis.files.all())
    current_file = next((item for item in files if item.is_current), None)
    payload: dict[str, Any] = {
        "id": str(thesis.pk),
        "referenceCode": thesis.reference_code,
        "type": thesis.type,
        "typeLabel": ThesisType(thesis.type).label,
        "title": thesis.title,
        "abstract": thesis.abstract,
        "journal": thesis.journal,
        "status": thesis.status,
        "statusLabel": ThesisStatus(thesis.status).label,
        "statusDescription": STATUS_DESCRIPTIONS.get(thesis.status, ""),
        "progressStep": WORKFLOW_STEPS.get(thesis.status, 1),
        "isPublic": thesis.is_public,
        "course": course_brief(thesis.course),
        "advisor": user_brief(thesis.advisor),
        "student": user_brief(main_author),
        "authors": [
            {
                "id": str(author.pk),
                "position": author.position,
                "isMainAuthor": author.is_main_author,
                "user": user_brief(author.author),
            }
            for author in thesis.authors.all()
        ],
        "keywords": keywords,
        "currentFile": thesis_file_payload(current_file) if current_file else None,
        "submittedAt": thesis.submitted_at.isoformat() if thesis.submitted_at else None,
        "advisorApprovedAt": thesis.advisor_approved_at.isoformat() if thesis.advisor_approved_at else None,
        "publishedAt": thesis.published_at.isoformat() if thesis.published_at else None,
        "createdAt": thesis.created_at.isoformat(),
        "updatedAt": thesis.updated_at.isoformat(),
        "permissions": thesis_permissions(thesis, viewer),
    }
    if include_history:
        reviews: list[dict[str, Any]] = []
        for thesis_file in files:
            for review in thesis_file.reviews.all():
                reviews.append(review_payload(review))
        reviews.sort(key=lambda item: item["reviewedAt"], reverse=True)
        payload["files"] = [thesis_file_payload(item) for item in files]
        payload["reviews"] = reviews
        payload["statusHistory"] = [status_history_payload(entry) for entry in thesis.status_history.all()]
        payload["documents"] = [document_payload(document) for document in thesis.documents.all()]
    return payload


def thesis_list_payload(thesis: Thesis, viewer: User | None = None) -> dict[str, Any]:
    """
    Serialize a thesis summary for lists and dashboards.

    Args:
        thesis: Thesis instance.
        viewer: Authenticated user or ``None`` for public access.

    Returns:
        dict[str, Any]: Compact thesis payload.
    """
    main_author = _prefetched_main_author(thesis)
    current_file = _prefetched_current_file(thesis)
    keywords = _prefetched_keywords(thesis)
    return {
        "id": str(thesis.pk),
        "referenceCode": thesis.reference_code,
        "title": thesis.title,
        "status": thesis.status,
        "statusLabel": ThesisStatus(thesis.status).label,
        "statusDescription": STATUS_DESCRIPTIONS.get(thesis.status, ""),
        "progressStep": WORKFLOW_STEPS.get(thesis.status, 1),
        "isPublic": thesis.is_public,
        "course": course_brief(thesis.course),
        "advisor": user_brief(thesis.advisor),
        "student": user_brief(main_author),
        "keywords": keywords,
        "currentVersion": current_file.version_number if current_file else None,
        "currentFile": {
            "id": str(current_file.pk),
            "sizeBytes": current_file.stored_file.size_bytes,
            "sha256": current_file.stored_file.sha256_hex,
            "uploadedAt": current_file.uploaded_at.isoformat(),
            "previewUrl": f"/api/v1/theses/{thesis.pk}/files/{current_file.pk}/download?disposition=inline",
            "downloadUrl": f"/api/v1/theses/{thesis.pk}/files/{current_file.pk}/download?disposition=attachment",
        } if current_file else None,
        "submittedAt": thesis.submitted_at.isoformat() if thesis.submitted_at else None,
        "updatedAt": thesis.updated_at.isoformat(),
        "advisorApprovedAt": thesis.advisor_approved_at.isoformat() if thesis.advisor_approved_at else None,
        "pendingReview": thesis.status in REVIEWABLE_STATUSES,
        "permissions": thesis_permissions(thesis, viewer),
    }


def public_theses_queryset() -> QuerySet[Thesis]:
    """
    Return the public repository queryset.

    Returns:
        QuerySet[Thesis]: Published theses that were not deleted.
    """
    return thesis_queryset().filter(
        is_public=True,
        status=ThesisStatus.PUBLISHED,
        published_at__isnull=False,
        deleted_at__isnull=True,
    )


def public_thesis_payload(thesis: Thesis) -> dict[str, Any]:
    """
    Serialize a published thesis for anonymous repository access.

    Args:
        thesis: Published thesis.

    Returns:
        dict[str, Any]: Public thesis payload.
    """
    payload = thesis_payload(thesis, viewer=None, include_history=False)
    current_file = thesis.files.filter(is_current=True).select_related("stored_file").first()
    payload["currentFile"] = thesis_file_payload(current_file) if current_file else None
    if current_file is not None:
        payload["currentFile"]["downloadUrl"] = f"/api/v1/public/theses/{thesis.pk}/download"
        payload["currentFile"]["previewUrl"] = f"/api/v1/public/theses/{thesis.pk}/download?disposition=inline"
    return payload


def public_theses_filtered(query: str = "", course_id: str = "", advisor_id: str = "", year: str = "", keyword: str = "") -> QuerySet[Thesis]:
    """
    Apply repository filters to published theses.

    Args:
        query: Free-text search across title, abstract, authors and advisor.
        course_id: Optional course identifier.
        advisor_id: Optional advisor identifier.
        year: Optional defense/publication year.
        keyword: Optional keyword filter.

    Returns:
        QuerySet[Thesis]: Filtered public queryset.
    """
    queryset = public_theses_queryset()
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(abstract__icontains=query)
            | Q(authors__author__name__icontains=query)
            | Q(advisor__name__icontains=query)
            | Q(keyword_links__keyword__name__icontains=query)
        )
    if course_id:
        queryset = queryset.filter(course_id=course_id)
    if advisor_id:
        queryset = queryset.filter(advisor_id=advisor_id)
    if year:
        try:
            parsed_year = int(year)
        except (TypeError, ValueError):
            parsed_year = None
        if parsed_year is not None:
            queryset = queryset.filter(published_at__year=parsed_year)
    if keyword:
        queryset = queryset.filter(keyword_links__keyword__normalized_name=keyword.strip().casefold())
    return queryset.distinct()
