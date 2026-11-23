"""
Tests of the academic identity fields exposed by the admin user API.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from academic.models import Department
from accounts.models import ProfileRole, User, UserProfileRole

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
class UserAcademicFieldsTests(TestCase):
    """Registration number, department and research area are editable by administrators."""

    def setUp(self) -> None:
        self.client = APIClient()
        self.admin = User.objects.create_superuser(email="admin@ufvjm.edu.br", name="Admin", password="pass")
        UserProfileRole.objects.create(user=self.admin, role=ProfileRole.ADMIN)
        self.department = Department.objects.create(name="Computação")
        self.client.force_authenticate(user=self.admin)

    def test_create_with_academic_fields(self) -> None:
        response = self.client.post(
            "/api/v1/admin/users",
            {
                "name": "Aluno Grupo",
                "email": "grupo@ufvjm.edu.br",
                "roles": [ProfileRole.STUDENT],
                "registrationNumber": "2026110001",
                "departmentId": str(self.department.pk),
                "researchArea": "Sistemas distribuídos",
            },
            format="json",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.data["registrationNumber"], "2026110001")
        self.assertEqual(response.data["departmentId"], str(self.department.pk))
        self.assertEqual(response.data["departmentName"], "Computação")
        self.assertEqual(response.data["researchArea"], "Sistemas distribuídos")

    def test_patch_academic_fields(self) -> None:
        user = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Aluno")
        UserProfileRole.objects.create(user=user, role=ProfileRole.STUDENT)
        response = self.client.patch(
            f"/api/v1/admin/users/{user.pk}",
            {"registrationNumber": "2026110002", "departmentId": str(self.department.pk), "researchArea": "Banco de dados"},
            format="json",
            REMOTE_ADDR="127.0.0.1",
        )
        self.assertEqual(response.status_code, 200, response.content)
        user.refresh_from_db()
        self.assertEqual(user.registration_number, "2026110002")
        self.assertEqual(user.department_id, self.department.pk)
        self.assertEqual(user.research_area, "Banco de dados")
