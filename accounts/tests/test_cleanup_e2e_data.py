"""Regression tests for the emergency E2E data cleanup command."""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from notifications.models import Notification, NotificationTemplateCode
from theses.models import Thesis, ThesisStatus

E2E_USER = "e2e-cleanup@ufvjm.edu.br"


class CleanupE2EDataTests(TestCase):
    """Only marked E2E rows are removed; cross-service writes stay alias-safe."""

    def test_removes_only_marked_rows(self) -> None:
        department = Department.objects.create(name="Departamento E2E cleanup")
        course = Course.objects.create(department=department, name="Curso E2E cleanup", code="E2ECLEAN")
        user = User.objects.create_user(email=E2E_USER, name="Seed Admin E2E cleanup")
        UserProfileRole.objects.create(user=user, role=ProfileRole.ADMIN)
        thesis = Thesis.objects.create(course_id=course.pk, advisor_id=user.pk, title="Tese E2E cleanup", status=ThesisStatus.SUBMITTED)
        template = Notification.objects.model._meta.get_field("template").related_model.objects.get(code=NotificationTemplateCode.THESIS_PUBLISHED)
        Notification.objects.create(template_id=template.pk, user_id=user.pk, subject="Aviso E2E", body="Corpo")

        real_department = Department.objects.create(name="Departamento Real")
        real_course = Course.objects.create(department=real_department, name="Curso Real", code="REAL1")

        call_command("cleanup_e2e_data", "--apply", stdout=StringIO())

        self.assertFalse(Thesis.objects.filter(pk=thesis.pk).exists())
        self.assertFalse(Course.objects.filter(pk=course.pk).exists())
        self.assertFalse(Notification.objects.filter(user_id=user.pk).exists())
        self.assertTrue(Course.objects.filter(pk=real_course.pk).exists())
