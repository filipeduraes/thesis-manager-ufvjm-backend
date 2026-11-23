"""
Tests of coordinator course assignment and consolidated audit logging.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from academic.models import Course, Department
from academic.services import create_course, update_course
from accounts.models import AuditLog, ProfileRole, User, UserProfileRole
from accounts.audit_identifiers import EntityType

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
class CoordinatorCourseAssignmentTests(TestCase):
    """Assigning a coordinator also sets their course, with one audit entry."""

    def setUp(self) -> None:
        self.admin = User.objects.create_superuser(email="admin@ufvjm.edu.br", name="Admin", password="pass")
        UserProfileRole.objects.create(user=self.admin, role=ProfileRole.ADMIN)
        self.department = Department.objects.create(name="Computação")
        self.client = APIClient()

    def test_course_creation_sets_coordinator_course_and_role(self) -> None:
        coordinator = User.objects.create_user(email="coord@ufvjm.edu.br", name="Coordenadora")

        course = create_course(name="Computação", code="CC", department_id=str(self.department.pk), coordinator_email=coordinator.email, request=None)
        coordinator.refresh_from_db()

        self.assertTrue(coordinator.has_active_role(ProfileRole.COORDINATOR))
        assignment = coordinator.course_assignments.filter(is_active=True, revoked_at__isnull=True).first()
        self.assertIsNotNone(assignment)
        self.assertEqual(assignment.course_id, course.pk)

    def test_course_update_replaces_coordinator_course(self) -> None:
        coordinator = User.objects.create_user(email="coord@ufvjm.edu.br", name="Coordenadora")
        first = create_course(name="Primeiro", code="C1", department_id=str(self.department.pk), coordinator_email=coordinator.email)
        second = create_course(name="Segundo", code="C2", department_id=str(self.department.pk))

        update_course(second, coordinator_email=coordinator.email)
        coordinator.refresh_from_db()

        assignment = coordinator.course_assignments.filter(is_active=True, revoked_at__isnull=True).first()
        self.assertEqual(assignment.course_id, second.pk)
        self.assertNotEqual(assignment.course_id, first.pk)

    def test_admin_patch_logs_course_change_once(self) -> None:
        target = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Aluno")
        course = create_course(name="Destino", code="C3", department_id=str(self.department.pk))
        logs_before = AuditLog.objects.count()
        self.client.force_authenticate(user=self.admin)

        response = self.client.patch(
            f"/api/v1/admin/users/{target.pk}",
            {"courseId": str(course.pk)},
            format="json",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 200, response.content)

        new_logs = list(AuditLog.objects.order_by("-id")[: AuditLog.objects.count() - logs_before])
        self.assertFalse(any(log.entity_type == EntityType.USER_COURSE_ASSIGNMENT for log in new_logs))
        user_logs = [log for log in new_logs if log.entity_type == EntityType.USER and log.entity_id == str(target.pk)]
        self.assertEqual(len(user_logs), 1)
        self.assertEqual(user_logs[0].changes["course"]["to"], course.name)
