"""
Course-scope enforcement when a student writes thesis metadata.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from academic.models import Course, Department, UserCourseAssignment
from accounts.models import ProfileRole, User, UserProfileRole

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=email.split("@")[0], password="senha123")
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ThesisCourseScopeTests(TestCase):
    """The server-side write path mirrors the course restriction of the form."""

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.allowed_course = Course.objects.create(name="CC", code="CC1", department=department)
        self.other_course = Course.objects.create(name="SI", code="SI1", department=department)
        self.advisor = create_user("advisor@ufvjm.edu.br", ProfileRole.ADVISOR)
        self.student = create_user("student@ufvjm.edu.br", ProfileRole.STUDENT)
        self.client = APIClient()
        self.client.force_authenticate(user=self.student)

    def payload(self, course_id: str) -> dict[str, object]:
        return {
            "title": "TCC",
            "abstract": "Resumo",
            "courseId": course_id,
            "advisorId": str(self.advisor.pk),
            "keywords": ["teste"],
        }

    def test_assigned_student_cannot_use_another_course(self) -> None:
        UserCourseAssignment.objects.create(user=self.student, course=self.allowed_course, is_active=True)
        response = self.client.put("/api/v1/theses/me", self.payload(str(self.other_course.pk)), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("curso da sua matrícula", str(response.data))

    def test_assigned_student_can_use_own_course(self) -> None:
        UserCourseAssignment.objects.create(user=self.student, course=self.allowed_course, is_active=True)
        response = self.client.put("/api/v1/theses/me", self.payload(str(self.allowed_course.pk)), format="json")
        self.assertEqual(response.status_code, 200)

    def test_unassigned_student_keeps_all_courses_fallback(self) -> None:
        response = self.client.put("/api/v1/theses/me", self.payload(str(self.other_course.pk)), format="json")
        self.assertEqual(response.status_code, 200)
