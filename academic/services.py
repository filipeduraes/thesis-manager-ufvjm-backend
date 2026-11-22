"""
Domain services for the academic registry.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpRequest
from django.utils import timezone

from academic.models import Course, Department, UserCourseAssignment
from accounts.concurrency import COURSE_ASSIGNMENT_LOCK_KEY, acquire_transaction_lock
from accounts.models import AuditAction, ProfileRole, User, UserProfileRole
from accounts.services import record_audit

logger = logging.getLogger(__name__)


def normalize_person_name(value: str) -> str:
    """
    Normalize a person or entity name for consistent storage.

    Args:
        value: Raw name value.

    Returns:
        str: Normalized name.
    """
    return " ".join((value or "").split())


def normalize_course_code(value: str) -> str:
    """
    Normalize a course code.

    Args:
        value: Raw course code.

    Returns:
        str: Normalized uppercase course code.
    """
    return (value or "").strip().upper()


def normalize_email(value: str | None) -> str:
    """
    Normalize an e-mail address.

    Args:
        value: Raw e-mail value.

    Returns:
        str: Normalized lowercase e-mail address.
    """
    return (value or "").strip().lower()


def ensure_coordinator_role(user: User) -> bool:
    """
    Ensure that the user has an active coordinator role.

    Args:
        user: Institutional user to update.

    Returns:
        bool: ``True`` when the role was created by this call.
    """
    _, created = UserProfileRole.objects.get_or_create(
        user=user,
        role=ProfileRole.COORDINATOR,
        revoked_at__isnull=True,
        defaults={"assigned_by": None},
    )
    return created


def resolve_pending_coordinators_for_user(
    user: User, request: HttpRequest | None = None
) -> list[str]:
    """
    Link pending courses to the user and grant coordinator access.

    Args:
        user: Institutional user that was created, found, or authenticated.
        request: HTTP request used for audit context.

    Returns:
        list[str]: Identifiers of updated courses.
    """
    email = normalize_email(user.email)
    if not email:
        return []

    with transaction.atomic():
        courses = list(
            Course.objects.select_for_update().filter(
                coordinator_email__iexact=email
            )
        )
        if not courses:
            return []

        updated_ids: list[str] = []
        role_created = False
        for course in courses:
            changed_fields: list[str] = []
            if course.coordinator_id != user.pk:
                course.coordinator = user
                changed_fields.append("coordinator")
            if normalize_email(course.coordinator_email) != email:
                course.coordinator_email = email
                changed_fields.append("coordinator_email")
            if changed_fields:
                course.save(update_fields=[*changed_fields, "updated_at"])
                updated_ids.append(str(course.pk))
                record_audit(
                    action=AuditAction.UPDATE,
                    entity_type="Course",
                    entity_id=str(course.pk),
                    entity_app_label="academic",
                    request=request,
                    actor_user=(
                        request.user
                        if request
                        and getattr(request.user, "is_authenticated", False)
                        and isinstance(request.user, User)
                        else None
                    ),
                    changes={
                        "coordinatorResolved": True,
                        "coordinatorUserId": str(user.pk),
                        "coordinatorEmail": email,
                    },
                    metadata={"trigger": "coordinator_resolution"},
                )

        if updated_ids:
            role_created = ensure_coordinator_role(user)

        if updated_ids:
            logger.info(
                "Resolved pending coordinator: user_id=%s courses=%s role_created=%s",
                user.pk,
                len(updated_ids),
                role_created,
            )
        return updated_ids


def resolve_course_coordinator(course: Course) -> User | None:
    """
    Resolve a course coordinator from the configured e-mail address.

    Args:
        course: Course instance to resolve.

    Returns:
        User | None: Matching user, if found.
    """
    email = normalize_email(course.coordinator_email)
    if not email:
        course.coordinator = None
        return None
    user = User.objects.filter(email__iexact=email).first()
    course.coordinator = user
    course.coordinator_email = email
    return user


def get_or_create_department(name: str) -> Department:
    """
    Get or create a department by normalized name.

    Args:
        name: Department name.

    Returns:
        Department: Existing or newly created department.

    Raises:
        ValidationError: If the name is empty.
    """
    normalized = normalize_person_name(name)
    if not normalized:
        raise ValidationError({"department": "Informe o nome do departamento."})
    department = Department.objects.filter(name__iexact=normalized).first()
    if department:
        return department
    try:
        return Department.objects.create(name=normalized)
    except IntegrityError:
        return Department.objects.get(name__iexact=normalized)


def prune_orphan_departments() -> int:
    """
    Remove departments that are not associated with any course.

    Returns:
        int: Number of deleted departments.
    """
    orphans = Department.objects.annotate(course_count=Count("courses")).filter(
        course_count=0
    )
    count = orphans.count()
    if count:
        orphans.delete()
        logger.info("Pruned orphan departments: count=%s", count)
    return count


def department_payload(department: Department) -> dict[str, Any]:
    """
    Serialize a department for administrative responses.

    Args:
        department: Department instance.

    Returns:
        dict[str, Any]: Serialized department payload.
    """
    return {
        "id": str(department.pk),
        "name": department.name,
    }


def coordinator_payload(course: Course) -> dict[str, Any] | None:
    """
    Serialize a course coordinator.

    Args:
        course: Course instance.

    Returns:
        dict[str, Any] | None: Coordinator payload or ``None``.
    """
    user = course.coordinator
    email = normalize_email(course.coordinator_email) or (
        user.email if user else ""
    )
    if not user and not email:
        return None
    status = "PENDING"
    if user and user.is_active:
        status = "FOUND_ACTIVE"
    elif user:
        status = "FOUND_INACTIVE"
    return {
        "id": str(user.pk) if user else None,
        "name": user.name if user else "",
        "email": email,
        "isActive": bool(user.is_active) if user else False,
        "status": status,
    }


def course_payload(course: Course) -> dict[str, Any]:
    """
    Serialize a course for the administrative API.

    Args:
        course: Course instance.

    Returns:
        dict[str, Any]: Serialized course payload.
    """
    coordinator = coordinator_payload(course)
    active_students = (
        course.user_assignments.filter(
            is_active=True,
            revoked_at__isnull=True,
            user__is_active=True,
            user__profile_roles__role=ProfileRole.STUDENT,
            user__profile_roles__revoked_at__isnull=True,
        )
        .values("user_id")
        .distinct()
        .count()
    )
    return {
        "id": str(course.pk),
        "name": course.name,
        "code": course.code,
        "department": department_payload(course.department),
        "departmentId": str(course.department_id),
        "coordinator": coordinator,
        "coordinatorEmail": normalize_email(course.coordinator_email)
        or (coordinator["email"] if coordinator else ""),
        "coordinatorStatus": (
            coordinator["status"] if coordinator else "PENDING"
        ),
        "activeStudents": active_students,
        "publishedTheses": 0,
    }


@transaction.atomic
def create_course(
    *,
    name: str,
    code: str,
    department_id: str | None = None,
    department_name: str | None = None,
    coordinator_email: str = "",
    request: HttpRequest | None = None,
) -> Course:
    """
    Create an administrative course.

    Args:
        name: Course name.
        code: Course code.
        department_id: Existing department identifier.
        department_name: Department name to select or create.
        coordinator_email: Coordinator e-mail address.
        request: HTTP request used for audit context.

    Returns:
        Course: Created course.

    Raises:
        ValidationError: If required fields are missing or invalid.
    """
    normalized_name = normalize_person_name(name)
    normalized_code = normalize_course_code(code)
    if not normalized_name:
        raise ValidationError({"name": "Informe o nome do curso."})
    if not normalized_code:
        raise ValidationError({"code": "Informe o código do curso."})

    if department_id:
        department = Department.objects.select_for_update().filter(pk=department_id).first()
        if not department:
            raise ValidationError({"departmentId": "Departamento inválido."})
    elif department_name:
        department = get_or_create_department(department_name)
    else:
        raise ValidationError({"department": "Informe o departamento."})

    if Course.objects.filter(code__iexact=normalized_code).exists():
        raise ValidationError({"code": "Já existe um curso com este código."})

    normalized_coordinator_email = normalize_email(coordinator_email)
    if normalized_coordinator_email:
        validate_email(normalized_coordinator_email)

    course = Course(
        name=normalized_name,
        code=normalized_code,
        department=department,
        coordinator_email=normalized_coordinator_email,
    )
    user = resolve_course_coordinator(course)
    try:
        course.save()
    except IntegrityError as exc:
        raise ValidationError({"code": "Já existe um curso com este código."}) from exc
    if user:
        ensure_coordinator_role(user)
        resolve_pending_coordinators_for_user(user, request=request)

    record_audit(
        action=AuditAction.CREATE,
        entity_type="Course",
        entity_id=str(course.pk),
        entity_app_label="academic",
        request=request,
        actor_user=(
            request.user
            if request
            and getattr(request.user, "is_authenticated", False)
            and isinstance(request.user, User)
            else None
        ),
        changes={
            "name": course.name,
            "code": course.code,
            "departmentId": str(course.department_id),
            "coordinatorEmail": course.coordinator_email,
        },
    )
    return course


@transaction.atomic
def update_course(
    course: Course,
    *,
    name: str | None = None,
    code: str | None = None,
    department_id: str | None = None,
    department_name: str | None = None,
    coordinator_email: str | None = None,
    request: HttpRequest | None = None,
) -> Course:
    """
    Update an administrative course.

    Args:
        course: Course instance to update.
        name: Updated course name.
        code: Updated course code.
        department_id: Updated department identifier.
        department_name: Updated department name.
        coordinator_email: Updated coordinator e-mail.
        request: HTTP request used for audit context.

    Returns:
        Course: Updated course.

    Raises:
        ValidationError: If required fields are missing or invalid.
    """
    # Serializa edições concorrentes do mesmo curso (evita perda de alterações).
    course = Course.objects.select_for_update().get(pk=course.pk)
    old_department_id = course.department_id
    changes: dict[str, Any] = {}

    if name is not None:
        normalized_name = normalize_person_name(name)
        if not normalized_name:
            raise ValidationError({"name": "Informe o nome do curso."})
        course.name = normalized_name
        changes["name"] = course.name
    if code is not None:
        normalized_code = normalize_course_code(code)
        if not normalized_code:
            raise ValidationError({"code": "Informe o código do curso."})
        if Course.objects.filter(code__iexact=normalized_code).exclude(
            pk=course.pk
        ).exists():
            raise ValidationError({"code": "Já existe um curso com este código."})
        course.code = normalized_code
        changes["code"] = course.code
    if department_id is not None or department_name is not None:
        if department_id:
            department = Department.objects.select_for_update().filter(
                pk=department_id
            ).first()
            if not department:
                raise ValidationError({"departmentId": "Departamento inválido."})
        else:
            department = get_or_create_department(str(department_name or ""))
        course.department = department
        changes["departmentId"] = str(course.department_id)
    if coordinator_email is not None:
        normalized_coordinator_email = normalize_email(coordinator_email)
        if normalized_coordinator_email:
            validate_email(normalized_coordinator_email)
        course.coordinator_email = normalized_coordinator_email
        user = resolve_course_coordinator(course)
        if user:
            ensure_coordinator_role(user)
        changes["coordinatorEmail"] = course.coordinator_email
        changes["coordinatorUserId"] = str(user.pk) if user else None

    course.save()
    if coordinator_email is not None and course.coordinator_id:
        resolve_pending_coordinators_for_user(course.coordinator, request=request)
    if old_department_id != course.department_id:
        prune_orphan_departments()

    if changes:
        record_audit(
            action=AuditAction.UPDATE,
            entity_type="Course",
            entity_id=str(course.pk),
            entity_app_label="academic",
            request=request,
            actor_user=(
                request.user
                if request
                and getattr(request.user, "is_authenticated", False)
                and isinstance(request.user, User)
                else None
            ),
            changes=changes,
        )
    return course


@transaction.atomic
def delete_course(course: Course, request: HttpRequest | None = None) -> None:
    """
    Delete a course and prune orphan departments when applicable.

    Args:
        course: Course instance to delete.
        request: HTTP request used for audit context.
    """
    course_id = str(course.pk)
    old_department_id = course.department_id
    snapshot = {
        "name": course.name,
        "code": course.code,
        "departmentId": str(course.department_id),
    }
    # User assignments are removed by FK cascade; thesis/publication models do not exist yet.
    course.delete()
    if old_department_id:
        prune_orphan_departments()
    record_audit(
        action=AuditAction.DELETE,
        entity_type="Course",
        entity_id=course_id,
        entity_app_label="academic",
        request=request,
        actor_user=(
            request.user
            if request
            and getattr(request.user, "is_authenticated", False)
            and isinstance(request.user, User)
            else None
        ),
        changes=snapshot,
    )


def set_user_course_assignment(
    user: User,
    course: Course | None,
    *,
    assigned_by: User | None = None,
    request: HttpRequest | None = None,
) -> None:
    """
    Set the active course assignment for a user.

    Args:
        user: User to update.
        course: Course to assign, or ``None`` to remove the active assignment.
        assigned_by: Administrator responsible for the change.
        request: HTTP request used for audit context.
    """
    with transaction.atomic():
        # Bloqueia o usuário (a linha "pai") para serializar atribuições
        # concorrentes, inclusive quando ainda não existe nenhuma atribuição.
        acquire_transaction_lock(COURSE_ASSIGNMENT_LOCK_KEY + (user.pk.int % 100000))
        User.objects.select_for_update().get(pk=user.pk)
        now = timezone.now()
        active = UserCourseAssignment.objects.select_for_update().filter(
            user=user,
            is_active=True,
        )
        if course is None:
            changed = active.exists()
            active.update(is_active=False, revoked_at=now)
            if changed:
                record_audit(
                    action=AuditAction.UPDATE,
                    entity_type="UserCourseAssignment",
                    entity_id=str(user.pk),
                    entity_app_label="academic",
                    request=request,
                    actor_user=assigned_by,
                    changes={"course": None},
                    metadata={
                        "userName": user.name,
                        "userEmail": user.email,
                    },
                )
            return

        current = active.filter(course=course).first()
        if current and active.count() == 1:
            return
        active.exclude(course=course).update(is_active=False, revoked_at=now)
        if not current:
            try:
                UserCourseAssignment.objects.create(
                    user=user,
                    course=course,
                    assigned_by=assigned_by,
                    is_active=True,
                )
            except IntegrityError:
                # Outra requisição já criou a atribuição equivalente.
                current = UserCourseAssignment.objects.filter(
                    user=user, course=course, is_active=True
                ).first()
                if current is None:
                    raise
        record_audit(
            action=AuditAction.UPDATE,
            entity_type="UserCourseAssignment",
            entity_id=str(user.pk),
            entity_app_label="academic",
            request=request,
            actor_user=assigned_by,
            changes={
                "courseId": str(course.pk),
                "course": course.name,
            },
            metadata={
                "userName": user.name,
                "userEmail": user.email,
            },
        )


def active_course_assignment(user: User) -> UserCourseAssignment | None:
    """
    Return the active course assignment for a user.

    Args:
        user: User instance.

    Returns:
        UserCourseAssignment | None: Active assignment, if present.
    """
    prefetched = getattr(user, "active_course_assignment_records", None)
    if prefetched is not None:
        return prefetched[0] if prefetched else None
    return (
        UserCourseAssignment.objects.select_related("course", "course__department")
        .filter(user=user, is_active=True, revoked_at__isnull=True)
        .first()
    )
