"""
Models for the academic registry (departments, courses, and user associations).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


class Department(models.Model):
    """
    Academic department used to organize courses.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """
        Database table and uniqueness configuration for departments.
        """

        db_table = "academic_department"
        verbose_name = "Departamento"
        verbose_name_plural = "Departamentos"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="academic_department_name_ci_unique",
            ),
        ]

    def __str__(self) -> str:
        """
        Return a human-readable representation.

        Returns:
            str: Department name.
        """
        return self.name

    def save(self, *args, **kwargs) -> None:
        """
        Normalize the department name before saving.

        Args:
            *args: Positional arguments forwarded to Django's save.
            **kwargs: Keyword arguments forwarded to Django's save.
        """
        if self.name:
            self.name = " ".join(self.name.split())
        super().save(*args, **kwargs)


class Course(models.Model):
    """
    Undergraduate course linked to a department and a coordinator.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="courses",
    )
    name = models.CharField(max_length=150)
    code = models.CharField(max_length=20)
    coordinator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="coordinated_courses",
    )
    coordinator_email = models.EmailField(max_length=254, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """
        Database table and uniqueness configuration for courses.
        """

        db_table = "academic_course"
        verbose_name = "Curso"
        verbose_name_plural = "Cursos"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                Lower("code"),
                name="academic_course_code_ci_unique",
            ),
        ]

    def __str__(self) -> str:
        """
        Return a human-readable representation.

        Returns:
            str: Course code and name.
        """
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs) -> None:
        """
        Normalize course fields before saving.

        Args:
            *args: Positional arguments forwarded to Django's save.
            **kwargs: Keyword arguments forwarded to Django's save.
        """
        if self.name:
            self.name = " ".join(self.name.split())
        if self.code:
            self.code = self.code.strip().upper()
        if self.coordinator_email:
            self.coordinator_email = self.coordinator_email.strip().lower()
        super().save(*args, **kwargs)


class UserCourseAssignment(models.Model):
    """
    Administrative association between a user and a course.

    The model keeps a simple history and enforces a single active assignment per user.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="course_assignments",
    )
    course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="user_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_course_memberships",
    )
    is_active = models.BooleanField(default=True)
    assigned_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """
        Database table, constraints, and indexes for user course assignments.
        """

        db_table = "academic_user_course_assignment"
        verbose_name = "Associação usuário-curso"
        verbose_name_plural = "Associações usuário-curso"
        ordering = ["-assigned_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=Q(is_active=True),
                name="academic_one_active_course_per_user",
            ),
        ]
        indexes = [
            models.Index(
                fields=["course", "is_active"],
                name="academic_course_active_idx",
            ),
            models.Index(
                fields=["user", "is_active"],
                name="academic_user_course_idx",
            ),
        ]

    def __str__(self) -> str:
        """
        Return a human-readable representation.

        Returns:
            str: User and course identifiers.
        """
        return f"{self.user_id}:{self.course_id}"
