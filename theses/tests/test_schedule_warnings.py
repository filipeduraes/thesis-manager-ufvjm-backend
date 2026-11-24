"""Tests of the red and yellow schedule conflict zones."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import ScheduleWarningError, add_institutional_member, defense_schedule_warnings
from theses.defense_scheduling import schedule_defense, update_defense
from theses.models import CommitteeRole, Defense, DefenseStatus, Thesis, ThesisStatus

START = datetime(2026, 12, 10, 14, 0, tzinfo=timezone.utc)


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


class ScheduleWarningTests(TestCase):
    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.course = Course.objects.create(department=department, name="CC", code="CC1")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.member = create_user("membro@ufvjm.edu.br", "Profa. Membro", ProfileRole.ADVISOR)
        self.thesis_a = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.admin.pk, title="Tese A", status=ThesisStatus.ADVISOR_APPROVED)
        self.thesis_b = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.admin.pk, title="Tese B", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense_a = schedule_defense(self.thesis_a, scheduled_datetime=START, actor=self.admin)
        add_institutional_member(self.defense_a, CommitteeRole.PRESIDENT, self.member, actor=self.admin)
        self.defense_b = schedule_defense(self.thesis_b, scheduled_datetime=START + timedelta(hours=3), actor=self.admin)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_hard_conflict_is_blocked_even_when_warning_is_acknowledged(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        with self.assertRaisesMessage(Exception, "já participa de outra banca"):
            add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, acknowledge_schedule_warning=True, actor=self.admin)

    def test_yellow_zone_requires_acknowledgement_before_adding_member(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        with self.assertRaises(ScheduleWarningError) as context:
            add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, actor=self.admin)
        self.assertEqual(context.exception.code, "SCHEDULE_WARNING")
        self.assertEqual(context.exception.warnings[0]["memberName"], "Profa. Membro")
        self.assertEqual(context.exception.warnings[0]["scheduledEndDatetime"], (START + timedelta(hours=2)).isoformat())
        self.assertEqual(self.defense_b.members.count(), 0)

        member = add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, acknowledge_schedule_warning=True, actor=self.admin)
        self.assertIsNotNone(member.pk)

    def test_free_slot_does_not_warn(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=3)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, actor=self.admin)
        self.assertEqual(defense_schedule_warnings(self.defense_b), [])

    def test_patch_into_yellow_zone_requires_acknowledgement(self) -> None:
        add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, actor=self.admin)
        new_start = START + timedelta(hours=2, minutes=30)
        with self.assertRaises(ScheduleWarningError):
            update_defense(self.defense_b, scheduled_datetime=new_start, actor=self.admin)
        self.defense_b.refresh_from_db()
        self.assertNotEqual(self.defense_b.scheduled_datetime, new_start)

        update_defense(self.defense_b, scheduled_datetime=new_start, acknowledge_schedule_warning=True, actor=self.admin)
        self.defense_b.refresh_from_db()
        self.assertEqual(self.defense_b.scheduled_datetime, new_start)

    @override_settings(DEFENSE_SCHEDULE_BUFFER_MINUTES=30)
    def test_buffer_is_configurable(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, actor=self.admin)
        self.assertEqual(defense_schedule_warnings(self.defense_b), [])

    @override_settings(DEFENSE_SCHEDULE_BUFFER_MINUTES="invalido")
    def test_invalid_buffer_falls_back_to_one_hour(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        with self.assertRaises(ScheduleWarningError):
            add_institutional_member(self.defense_b, CommitteeRole.MEMBER, self.member, actor=self.admin)

    def test_external_member_obeys_the_same_yellow_zone(self) -> None:
        invitation_a = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        self.defense_a.members.create(role=CommitteeRole.MEMBER, external_invitation_id=invitation_a.pk, name="Dr. Externo", email="ext@example.com")
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        response = self.client.post(
            f"/api/v1/defenses/{self.defense_b.pk}/members/external",
            {"role": CommitteeRole.MEMBER, "name": "Dr. Externo", "email": "ext@example.com", "institution": "UFMG"},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "SCHEDULE_WARNING")
        self.assertNotIn("ext@example.com", str(response.data))

    def test_detail_warnings_are_visible_only_to_managers(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        self.defense_b.members.create(role=CommitteeRole.MEMBER, institutional_member_id=self.member.pk, name=self.member.name, email=self.member.email)

        admin_response = self.client.get(f"/api/v1/defenses/{self.defense_b.pk}")
        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(len(admin_response.data["defense"]["scheduleWarnings"]), 1)

        self.client.force_authenticate(user=self.member)
        member_response = self.client.get(f"/api/v1/defenses/{self.defense_b.pk}")
        self.assertEqual(member_response.status_code, 200)
        self.assertEqual(member_response.data["defense"]["scheduleWarnings"], [])

    def test_api_returns_409_with_warning_code(self) -> None:
        self.defense_b.scheduled_datetime = START + timedelta(hours=2, minutes=30)
        self.defense_b.save(update_fields=["scheduled_datetime"])
        response = self.client.post(
            f"/api/v1/defenses/{self.defense_b.pk}/members",
            {"role": CommitteeRole.MEMBER, "userId": str(self.member.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "SCHEDULE_WARNING")
        self.assertEqual(len(response.data["warnings"]), 1)
        self.assertNotIn("membro@ufvjm.edu.br", str(response.data))

        response = self.client.post(
            f"/api/v1/defenses/{self.defense_b.pk}/members",
            {"role": CommitteeRole.MEMBER, "userId": str(self.member.pk), "acknowledgeScheduleWarning": True},
            format="json",
        )
        self.assertEqual(response.status_code, 201)
