"""
Management of the public repository: unpublication and protected deletion.

Administrators manage every course; coordinators manage only the courses
linked to them by ``coordinated_courses_filter`` (the same rule used for the
thesis lists). Every change records who, what and when in
``ThesisStatusHistory`` and ``AuditLog`` and invalidates the public cache.
"""

from __future__ import annotations

import logging

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from academic.models import Course
from accounts.models import AuditAction, User
from theses.models import ACTIVE_DEFENSE_STATUSES, PUBLISHED_DELETE_ERROR, Thesis, ThesisStatus, thesis_was_published
from theses.repository_cache import invalidate_public_repository_cache
from theses.services import _add_status_history, _audit_thesis, coordinated_courses_filter, user_is_admin, user_is_coordinator

logger = logging.getLogger(__name__)

UNPUBLISH_REASON_MAX_LENGTH = 1000


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
