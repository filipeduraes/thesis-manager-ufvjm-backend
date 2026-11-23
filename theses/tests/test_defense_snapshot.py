"""Tests of the bibliographic snapshot stored with a scheduled defense."""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_scheduling import schedule_defense
from theses.models import Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES)
class DefenseSnapshotTests(TestCase):
    """The committee owns a copy of the thesis identity at scheduling time."""

    def test_snapshot_is_filled_on_schedule(self) -> None:
        admin = User.objects.create_user(email="admin@ufvjm.edu.br", password="senha123", name="Admin")
        UserProfileRole.objects.create(user=admin, role=ProfileRole.ADMIN)
        advisor = User.objects.create_user(email="adv@ufvjm.edu.br", password="senha123", name="Orientadora")
        UserProfileRole.objects.create(user=advisor, role=ProfileRole.ADVISOR)
        department = Department.objects.create(name="Computação")
        course = Course.objects.create(department=department, name="Computação", code="CC")
        thesis = Thesis.objects.create(course=course, advisor=advisor, title="Tese com snapshot", status=ThesisStatus.ADVISOR_APPROVED)

        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=admin)

        self.assertEqual(defense.thesis_snapshot["title"], "Tese com snapshot")
        self.assertEqual(defense.thesis_snapshot["courseCode"], "CC")
        self.assertEqual(defense.thesis_snapshot["advisorName"], "Orientadora")
