"""
Management of the public repository: unpublication and protected deletion.

Administrators manage every course; coordinators manage only the courses
linked to them by ``coordinated_courses_filter`` (the same rule used for the
thesis lists). Every change records who, what and when in
``ThesisStatusHistory`` and ``AuditLog`` and invalidates the public cache.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from academic.models import Course
from accounts.models import AuditAction, ProfileRole, User
from theses.models import ACTIVE_DEFENSE_STATUSES, PUBLISHED_DELETE_ERROR, DefenseResult, Thesis, ThesisAuthor, ThesisFile, ThesisOrigin, ThesisStatus, ThesisType, thesis_was_published
from theses.repository_cache import invalidate_public_repository_cache
from theses.services import (
    REPOSITORY_TIME_ZONE,
    _add_status_history,
    _audit_thesis,
    _store_pdf,
    _sync_keywords,
    _validate_pdf_upload,
    coordinated_courses_filter,
    normalize_keyword_names,
    user_is_admin,
    user_is_coordinator,
)

logger = logging.getLogger(__name__)

UNPUBLISH_REASON_MAX_LENGTH = 1000
MIN_DEFENSE_YEAR = 1950
LEGACY_RESULTS: tuple[str, ...] = (DefenseResult.APPROVED, DefenseResult.APPROVED_WITH_REMARKS)


def user_manages_course(user: User, course: Course) -> bool:
    """
    Return whether a user may manage the repository entries of a course.

    Args:
        user: Institutional user.
        course: Course of the thesis.

    Returns:
        bool: ``True`` for administrators and for coordinators of the course.
    """
    if user_is_admin(user):
        return True
    if not user_is_coordinator(user):
        return False
    return Course.objects.filter(pk=course.pk).filter(coordinated_courses_filter(user)).exists()


def ensure_manages_course(user: User, course: Course) -> None:
    """
    Require that a user manages the repository entries of a course.

    Args:
        user: Institutional user.
        course: Course of the thesis.

    Raises:
        PermissionDenied: When the user is neither administrator nor coordinator of the course.
    """
    if not user_manages_course(user, course):
        raise PermissionDenied("Apenas administradores ou a coordenação do curso podem gerenciar esta monografia.")


@dataclass(frozen=True)
class LegacyAuthor:
    """
    Author of a legacy thesis: a display name, optionally linked to a user.

    Attributes:
        name: Name as printed in the original work (falls back to the user's name).
        user: Linked institutional user, when the author has an account.
    """

    name: str
    user: User | None = None


@dataclass(frozen=True)
class ThesisMetadataInput:
    """
    Bibliographic metadata of a published or legacy thesis.

    Attributes:
        title: Thesis title.
        abstract: Abstract.
        keywords: Keyword names.
        authors: Authors in order; the first one is the main author.
        advisor: Advisor user.
        course: Course.
        defense_year: Defense year.
        defense_date: Optional exact defense date.
        thesis_type: ``ThesisType`` code.
        journal: Journal name for articles.
    """

    title: str
    abstract: str
    keywords: list[str]
    authors: list[LegacyAuthor]
    advisor: User
    course: Course
    defense_year: int
    defense_date: date | None = None
    thesis_type: str = ThesisType.MONOGRAPH
    journal: str = ""


def _validate_advisor_for_repository(advisor: User) -> None:
    """
    Require a user who has or had the advisor role.

    Legacy theses may have been advised by professors whose role was later
    revoked, so the role history counts, not only active roles.

    Args:
        advisor: Candidate advisor.

    Raises:
        ValidationError: When the user never had the advisor role.
    """
    if not advisor.profile_roles.filter(role=ProfileRole.ADVISOR).exists():
        raise ValidationError({"advisorId": "Selecione um usuário com papel de orientador."})


def _validate_defense_period(defense_year: int, defense_date: date | None) -> None:
    """
    Validate the defense year/date of a repository entry.

    Args:
        defense_year: Defense year.
        defense_date: Optional exact defense date.

    Raises:
        ValidationError: When the year is out of range, the date is in the future
            or the date does not match the year.
    """
    today = timezone.localdate(timezone=REPOSITORY_TIME_ZONE)
    if defense_year < MIN_DEFENSE_YEAR or defense_year > today.year:
        raise ValidationError({"defenseYear": f"Informe um ano de defesa entre {MIN_DEFENSE_YEAR} e {today.year}."})
    if defense_date is not None:
        if defense_date > today:
            raise ValidationError({"defenseDate": "A data de defesa não pode estar no futuro."})
        if defense_date.year != defense_year:
            raise ValidationError({"defenseDate": "A data de defesa não corresponde ao ano informado."})


def _clean_metadata(metadata: ThesisMetadataInput) -> ThesisMetadataInput:
    """
    Normalize and validate repository metadata shared by import and edition.

    Args:
        metadata: Raw metadata.

    Returns:
        ThesisMetadataInput: Metadata with collapsed whitespace and clean keywords.

    Raises:
        ValidationError: When a required field is missing or invalid.
    """
    title = " ".join((metadata.title or "").split())
    if not title:
        raise ValidationError({"title": "Informe o título da monografia."})
    abstract = (metadata.abstract or "").strip()
    if not abstract:
        raise ValidationError({"abstract": "Informe o resumo da monografia."})
    keywords = normalize_keyword_names(metadata.keywords)
    if not keywords:
        raise ValidationError({"keywords": "Informe pelo menos uma palavra-chave."})
    authors = _clean_authors(metadata.authors)
    _validate_advisor_for_repository(metadata.advisor)
    _validate_defense_period(metadata.defense_year, metadata.defense_date)
    return replace(metadata, title=title, abstract=abstract, keywords=keywords, authors=authors, journal=(metadata.journal or "").strip())


def _clean_authors(authors: list[LegacyAuthor]) -> list[LegacyAuthor]:
    """
    Normalize author names and reject empty, repeated or excessive authors.

    Args:
        authors: Raw authors in order.

    Returns:
        list[LegacyAuthor]: Authors with a non-empty display name.

    Raises:
        ValidationError: When there is no author, too many authors or duplicates.
    """
    maximum = getattr(settings, "THESIS_MAX_AUTHORS", 5)
    if not authors:
        raise ValidationError({"authors": "Informe pelo menos um autor."})
    if len(authors) > maximum:
        raise ValidationError({"authors": f"Informe no máximo {maximum} autores."})
    cleaned: list[LegacyAuthor] = []
    seen_users: set[object] = set()
    seen_names: set[str] = set()
    for author in authors:
        name = " ".join((author.name or "").split()) or (author.user.name if author.user else "")
        if not name:
            raise ValidationError({"authors": "Informe o nome de cada autor."})
        if author.user is not None:
            if author.user.pk in seen_users:
                raise ValidationError({"authors": "O mesmo usuário foi informado mais de uma vez como autor."})
            seen_users.add(author.user.pk)
        if name.casefold() in seen_names:
            raise ValidationError({"authors": "O mesmo autor foi informado mais de uma vez."})
        seen_names.add(name.casefold())
        cleaned.append(LegacyAuthor(name=name[:200], user=author.user))
    return cleaned


def _replace_authors(thesis: Thesis, authors: list[LegacyAuthor]) -> None:
    """
    Replace the author list of a thesis, keeping the first author as main author.

    Args:
        thesis: Locked thesis.
        authors: Clean authors in order.
    """
    thesis.authors.all().delete()
    for position, author in enumerate(authors, start=1):
        ThesisAuthor.objects.create(thesis=thesis, author=author.user, name=author.name, position=position, is_main_author=position == 1)


def _ensure_pdf_not_in_other_thesis(digest: bytes, thesis: Thesis | None = None) -> None:
    """
    Refuse a PDF that is already a version of another thesis.

    The SHA-256 deduplicates storage; for repository entries it also prevents the
    same work from being registered twice.

    Args:
        digest: SHA-256 of the uploaded PDF.
        thesis: Thesis receiving the file, whose own versions are allowed.

    Raises:
        ValidationError: When another non-deleted thesis already has this PDF.
    """
    versions = ThesisFile.objects.filter(stored_file__sha256=digest, thesis__deleted_at__isnull=True)
    if thesis is not None:
        versions = versions.exclude(thesis=thesis)
    if versions.exists():
        raise ValidationError({"file": "Este PDF já está cadastrado em outra monografia."})


@transaction.atomic
def import_legacy_thesis(user: User, metadata: ThesisMetadataInput, result: str, uploaded_file: Any, request: HttpRequest | None = None) -> Thesis:
    """
    Register an approved thesis that did not go through the system workflow.

    The PDF goes through the same validation as ordinary uploads (PDF only,
    size limit, SHA-256 and deduplication) and stays ``PENDING`` for the
    existing text extraction. The thesis is published at once with origin
    ``LEGACY_IMPORT``; no ``thesis.published`` event is emitted, so authors and
    advisors of old works are not e-mailed.

    Args:
        user: Administrator or coordinator of the course.
        metadata: Bibliographic metadata.
        result: ``APPROVED`` or ``APPROVED_WITH_REMARKS``.
        uploaded_file: Uploaded PDF.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Published legacy thesis.

    Raises:
        PermissionDenied: When the user does not manage the course.
        ValidationError: When the metadata, result or file is invalid.
    """
    ensure_manages_course(user, metadata.course)
    if result not in LEGACY_RESULTS:
        raise ValidationError({"result": "Apenas monografias aprovadas podem ser cadastradas no repositório."})
    clean = _clean_metadata(metadata)
    digest, _ = _validate_pdf_upload(uploaded_file, "THESIS_MAX_PDF_SIZE_MB", 50)
    _ensure_pdf_not_in_other_thesis(digest)
    now = timezone.now()
    defense_date = clean.defense_date
    thesis = Thesis.objects.create(
        course=clean.course,
        advisor=clean.advisor,
        type=clean.thesis_type,
        title=clean.title,
        abstract=clean.abstract,
        journal=clean.journal,
        status=ThesisStatus.PUBLISHED,
        is_public=True,
        origin=ThesisOrigin.LEGACY_IMPORT,
        published_at=now,
        defense_date=defense_date,
        defense_year=clean.defense_year,
    )
    _replace_authors(thesis, clean.authors)
    _sync_keywords(thesis, clean.keywords)
    stored_file = _store_pdf(uploaded_file, thesis, user, "THESIS_MAX_PDF_SIZE_MB", 50)
    ThesisFile.objects.create(thesis=thesis, stored_file=stored_file, version_number=1, is_current=True, change_description="Versão publicada (cadastro de acervo).", uploaded_by=user, hash_verified_at=now)
    history_metadata = {"event": "LEGACY_IMPORT", "origin": ThesisOrigin.LEGACY_IMPORT, "result": result, "versionNumber": 1, "sha256": stored_file.sha256_hex}
    _add_status_history(thesis, "", ThesisStatus.PUBLISHED, user, "Monografia cadastrada manualmente a partir do acervo anterior ao sistema.", metadata=history_metadata)
    audit_metadata = {"event": "LEGACY_IMPORT", "result": result, "defenseYear": clean.defense_year, "sha256": stored_file.sha256_hex, "sizeBytes": stored_file.size_bytes}
    _audit_thesis(AuditAction.CREATE, thesis, request, user, metadata=audit_metadata)
    invalidate_public_repository_cache()
    logger.info("Legacy thesis %s imported by %s", thesis.pk, user.pk)
    return thesis


@transaction.atomic
def unpublish_thesis(user: User, thesis: Thesis, reason: str, request: HttpRequest | None = None) -> Thesis:
    """
    Remove a published thesis from the public repository.

    The thesis moves to ``ARCHIVED`` (so the automatic publication does not put
    it back) and keeps ``published_at``, which also keeps it protected from
    deletion. Files and history are preserved.

    Args:
        user: Administrator or coordinator of the course.
        thesis: Published thesis.
        reason: Mandatory justification recorded in history and audit.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Archived thesis.

    Raises:
        PermissionDenied: When the user does not manage the course.
        ValidationError: When the thesis is not published or the reason is missing.
    """
    thesis = Thesis.objects.select_for_update().select_related("course", "advisor").get(pk=thesis.pk)
    ensure_manages_course(user, thesis.course)
    clean_reason = " ".join((reason or "").split())
    if not clean_reason:
        raise ValidationError({"reason": "Informe o motivo da despublicação."})
    if len(clean_reason) > UNPUBLISH_REASON_MAX_LENGTH:
        raise ValidationError({"reason": f"O motivo deve ter no máximo {UNPUBLISH_REASON_MAX_LENGTH} caracteres."})
    if thesis.status != ThesisStatus.PUBLISHED:
        raise ValidationError("Apenas monografias publicadas podem ser despublicadas.")
    previous_status = thesis.status
    thesis.status = ThesisStatus.ARCHIVED
    thesis.is_public = False
    thesis.save(update_fields=["status", "is_public", "updated_at"])
    _add_status_history(thesis, previous_status, ThesisStatus.ARCHIVED, user, clean_reason, metadata={"event": "UNPUBLISHED"})
    _audit_thesis(AuditAction.UPDATE, thesis, request, user, metadata={"event": "UNPUBLISHED", "reason": clean_reason}, changes={"status": {"from": previous_status, "to": ThesisStatus.ARCHIVED}, "isPublic": {"from": True, "to": False}})
    invalidate_public_repository_cache()
    logger.info("Thesis %s unpublished by %s", thesis.pk, user.pk)
    return thesis


@transaction.atomic
def delete_thesis(user: User, thesis: Thesis, request: HttpRequest | None = None) -> Thesis:
    """
    Soft-delete a thesis that was never published.

    Published (or unpublished) theses can never be deleted, not even by an
    administrator; they can only be unpublished.

    Args:
        user: Administrator or coordinator of the course.
        thesis: Thesis to delete.
        request: HTTP request used for audit context.

    Returns:
        Thesis: Thesis with ``deleted_at`` set.

    Raises:
        PermissionDenied: When the user does not manage the course.
        ValidationError: When the thesis is or was published, or has an active defense.
    """
    thesis = Thesis.objects.select_for_update().select_related("course", "advisor").get(pk=thesis.pk)
    ensure_manages_course(user, thesis.course)
    if thesis_was_published(thesis):
        raise ValidationError(PUBLISHED_DELETE_ERROR)
    if thesis.defenses.filter(status__in=ACTIVE_DEFENSE_STATUSES).exists():
        raise ValidationError("Cancele a banca ativa antes de excluir a monografia.")
    if thesis.deleted_at is None:
        thesis.deleted_at = timezone.now()
        thesis.save(update_fields=["deleted_at", "updated_at"])
        _audit_thesis(AuditAction.DELETE, thesis, request, user, metadata={"event": "THESIS_SOFT_DELETED", "status": thesis.status})
    return thesis
