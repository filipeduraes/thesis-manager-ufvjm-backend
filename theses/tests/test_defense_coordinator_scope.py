"""
Tests restricting coordinators to the defenses of the courses they coordinate.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department, UserCourseAssignment
from accounts.models import ProfileRole, User, UserProfileRole
from theses.defense_committee import _can_manage_defense, add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, Defense, DefenseStatus, Thesis, ThesisAuthor, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class CoordinatorDefenseScopeTests(TestCase):
    """Coordinator of course A manages course A defenses only; admin keeps full access."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator_a = create_user("coord.a@ufvjm.edu.br", "Coord A", ProfileRole.COORDINATOR)
        self.coordinator_b = create_user("coord.b@ufvjm.edu.br", "Coord B", ProfileRole.COORDINATOR)
        self.course_a = Course.objects.create(department=self.department, coordinator=self.coordinator_a, name="CC", code="CC001")
        self.course_b = Course.objects.create(department=self.department, coordinator=self.coordinator_b, name="SI", code="SI001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.thesis_a = Thesis.objects.create(course_id=self.course_a.pk, advisor_id=self.advisor.pk, title="Tese A", status=ThesisStatus.ADVISOR_APPROVED)
        self.schedulable_a = Thesis.objects.create(course_id=self.course_a.pk, advisor_id=self.advisor.pk, title="Tese A2", status=ThesisStatus.READY_FOR_DEFENSE)
        self.defense_a = schedule_defense(self.thesis_a, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.client = APIClient()

    def _as(self, user: User) -> APIClient:
        self.client.force_authenticate(user=user)
        return self.client

    # --- coordenador do curso ---

    def test_course_coordinator_lists_and_manages(self) -> None:
        client = self._as(self.coordinator_a)
        listing = client.get("/api/v1/defenses")
        self.assertEqual({item["id"] for item in listing.data["results"]}, {str(self.defense_a.pk)})
        schedulable = client.get("/api/v1/defenses/schedulable-theses")
        self.assertEqual({item["id"] for item in schedulable.data["results"]}, {str(self.schedulable_a.pk)})
        self.assertEqual(client.get(f"/api/v1/defenses/{self.defense_a.pk}").status_code, 200)
        self.assertEqual(client.patch(f"/api/v1/defenses/{self.defense_a.pk}", {"notes": "ok"}, format="json").status_code, 200)
        schedule = client.post(
            f"/api/v1/theses/{self.schedulable_a.pk}/defenses",
            {
                "scheduledDatetime": (timezone.now() + timedelta(days=9)).isoformat(),
                "members": [{"role": CommitteeRole.PRESIDENT, "userId": str(self.advisor.pk)}],
            },
            format="json",
        )
        self.assertEqual(schedule.status_code, 201)

    def test_coordinator_via_coordinator_email(self) -> None:
        coordinator = create_user("coord.email@ufvjm.edu.br", "Coord Email", ProfileRole.COORDINATOR)
        Course.objects.filter(pk=self.course_a.pk).update(coordinator=None, coordinator_email="COORD.EMAIL@ufvjm.edu.br")
        self.assertTrue(_can_manage_defense(coordinator, self.thesis_a.pk))

    def test_coordinator_via_active_course_assignment(self) -> None:
        coordinator = create_user("coord.assign@ufvjm.edu.br", "Coord Vínculo", ProfileRole.COORDINATOR)
        assignment = UserCourseAssignment.objects.create(user=coordinator, course=self.course_a)
        self.assertTrue(_can_manage_defense(coordinator, self.thesis_a.pk))
        assignment.is_active = False
        assignment.revoked_at = timezone.now()
        assignment.save(update_fields=["is_active", "revoked_at"])
        self.assertFalse(_can_manage_defense(coordinator, self.thesis_a.pk))

    # --- coordenador de outro curso ---

    def test_other_course_coordinator_gets_empty_lists(self) -> None:
        client = self._as(self.coordinator_b)
        self.assertEqual(client.get("/api/v1/defenses").data["count"], 0)
        self.assertEqual(client.get("/api/v1/defenses/schedulable-theses").data["count"], 0)

    def test_other_course_coordinator_cannot_read_or_act(self) -> None:
        client = self._as(self.coordinator_b)
        base = f"/api/v1/defenses/{self.defense_a.pk}"
        self.assertEqual(client.get(base).status_code, 404)
        self.assertEqual(client.patch(base, {"notes": "x"}, format="json").status_code, 404)
        self.assertEqual(client.post(f"{base}/start", {}, format="json").status_code, 404)
        self.assertEqual(client.post(f"{base}/close", {}, format="json").status_code, 404)
        self.assertEqual(client.post(f"{base}/cancel", {}, format="json").status_code, 404)
        self.assertEqual(client.post(f"{base}/members", {"role": CommitteeRole.MEMBER, "userId": str(self.advisor.pk)}, format="json").status_code, 404)
        self.assertEqual(client.get(f"/api/v1/theses/{self.thesis_a.pk}/defenses").status_code, 404)
        schedule = client.post(f"/api/v1/theses/{self.schedulable_a.pk}/defenses", {"scheduledDatetime": (timezone.now() + timedelta(days=9)).isoformat()}, format="json")
        self.assertEqual(schedule.status_code, 404)
        self.assertEqual(Defense.objects.get(pk=self.defense_a.pk).status, DefenseStatus.SCHEDULED)

    def test_other_course_coordinator_service_denied(self) -> None:
        with self.assertRaises(PermissionDenied):
            schedule_defense(self.schedulable_a, scheduled_datetime=timezone.now() + timedelta(days=9), actor=self.coordinator_b)

    def test_other_course_coordinator_on_committee_sees_but_cannot_manage(self) -> None:
        add_institutional_member(self.defense_a, CommitteeRole.MEMBER, self.coordinator_b, actor=self.admin)
        client = self._as(self.coordinator_b)
        self.assertEqual(client.get("/api/v1/defenses").data["count"], 1)
        self.assertEqual(client.get(f"/api/v1/defenses/{self.defense_a.pk}").status_code, 200)
        self.assertEqual(client.post(f"/api/v1/defenses/{self.defense_a.pk}/start", {}, format="json").status_code, 403)
        self.assertEqual(client.patch(f"/api/v1/defenses/{self.defense_a.pk}", {"notes": "x"}, format="json").status_code, 403)

    def test_coordinator_cannot_manage_own_authored_thesis(self) -> None:
        ThesisAuthor.objects.create(thesis=self.schedulable_a, author_id=self.coordinator_a.pk, is_main_author=True)
        self.assertFalse(_can_manage_defense(self.coordinator_a, self.schedulable_a.pk))
        self.assertTrue(_can_manage_defense(self.coordinator_a, self.thesis_a.pk))

    # --- admin e orientador ---

    def test_admin_still_sees_everything(self) -> None:
        thesis_b = Thesis.objects.create(course_id=self.course_b.pk, advisor_id=self.advisor.pk, title="Tese B", status=ThesisStatus.ADVISOR_APPROVED)
        schedule_defense(thesis_b, scheduled_datetime=timezone.now() + timedelta(days=8), actor=self.admin)
        client = self._as(self.admin)
        self.assertEqual(client.get("/api/v1/defenses").data["count"], 2)
        self.assertTrue(_can_manage_defense(self.admin, thesis_b.pk))

    def test_advisor_rules_unchanged(self) -> None:
        other_advisor = create_user("other@ufvjm.edu.br", "Outro", ProfileRole.ADVISOR)
        add_institutional_member(self.defense_a, CommitteeRole.MEMBER, other_advisor, actor=self.admin)
        self.assertTrue(_can_manage_defense(self.advisor, self.thesis_a.pk))
        self.assertFalse(_can_manage_defense(other_advisor, self.thesis_a.pk))
        client = self._as(other_advisor)
        self.assertEqual(client.get("/api/v1/defenses").data["count"], 1)
