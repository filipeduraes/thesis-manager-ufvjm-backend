"""Atomic defense scheduling: members are part of the same transaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from django.test import TestCase
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ProfileRole, User, UserProfileRole
from theses.models import CommitteeRole, Defense, Thesis, ThesisStatus

START = datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc)


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


class AtomicSchedulingTests(TestCase):
    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.course = Course.objects.create(department=department, name="CC", code="CC1")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.member = create_user("orientador@ufvjm.edu.br", "Orientador José", ProfileRole.ADVISOR)
        self.thesis_a = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.admin.pk, title="Tese A", status=ThesisStatus.ADVISOR_APPROVED)
        self.thesis_b = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.admin.pk, title="Tese B", status=ThesisStatus.ADVISOR_APPROVED)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def payload(self, when: datetime, members: list[dict[str, object]]) -> dict[str, object]:
        return {
            "scheduledDatetime": when.isoformat(),
            "durationMinutes": 60,
            "modality": "IN_PERSON",
            "location": "Sala 1",
            "members": members,
        }

    def member_payload(self, role: str = CommitteeRole.PRESIDENT) -> dict[str, object]:
        return {"role": role, "userId": str(self.member.pk)}

    def test_schedule_requires_at_least_one_member(self) -> None:
        response = self.client.post(f"/api/v1/theses/{self.thesis_a.pk}/defenses", self.payload(START, []), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Defense.objects.count(), 0)

    def test_hard_conflict_blocks_the_whole_schedule(self) -> None:
        first = self.client.post(f"/api/v1/theses/{self.thesis_a.pk}/defenses", self.payload(START, [self.member_payload()]), format="json")
        self.assertEqual(first.status_code, 201)

        second = self.client.post(f"/api/v1/theses/{self.thesis_b.pk}/defenses", self.payload(START, [self.member_payload()]), format="json")
        self.assertEqual(second.status_code, 400)
        self.assertIn("já participa de outra banca", str(second.data))
        self.assertEqual(Defense.objects.count(), 1)
        self.assertEqual(Defense.objects.get().members.count(), 1)

    def test_yellow_conflict_requires_acknowledgement(self) -> None:
        first = self.client.post(f"/api/v1/theses/{self.thesis_a.pk}/defenses", self.payload(START, [self.member_payload()]), format="json")
        self.assertEqual(first.status_code, 201)

        when = START + timedelta(minutes=90)
        second = self.client.post(f"/api/v1/theses/{self.thesis_b.pk}/defenses", self.payload(when, [self.member_payload()]), format="json")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.data["code"], "SCHEDULE_WARNING")
        self.assertEqual(Defense.objects.count(), 1)

        confirmed = self.payload(when, [self.member_payload()])
        confirmed["acknowledgeScheduleWarning"] = True
        second = self.client.post(f"/api/v1/theses/{self.thesis_b.pk}/defenses", confirmed, format="json")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(Defense.objects.count(), 2)

    def test_schedule_creates_members_and_events_together(self) -> None:
        response = self.client.post(f"/api/v1/theses/{self.thesis_a.pk}/defenses", self.payload(START, [self.member_payload()]), format="json")
        self.assertEqual(response.status_code, 201)
        defense = Defense.objects.get()
        self.assertEqual(defense.members.count(), 1)
        self.assertEqual(response.data["defense"]["id"], str(defense.pk))

    def test_schedule_check_reports_red_and_yellow_with_member_name(self) -> None:
        self.client.post(f"/api/v1/theses/{self.thesis_a.pk}/defenses", self.payload(START, [self.member_payload()]), format="json")
        red = self.client.post("/api/v1/defenses/check-schedule", self.payload(START, [self.member_payload()]), format="json")
        self.assertEqual(red.status_code, 200)
        self.assertEqual(red.data["conflicts"][0]["level"], "RED")
        self.assertIn("Orientador José", red.data["conflicts"][0]["message"])

        yellow = self.client.post("/api/v1/defenses/check-schedule", self.payload(START + timedelta(minutes=90), [self.member_payload()]), format="json")
        self.assertEqual(yellow.status_code, 200)
        self.assertEqual(yellow.data["conflicts"][0]["level"], "YELLOW")

        free = self.client.post("/api/v1/defenses/check-schedule", self.payload(START + timedelta(hours=3), [self.member_payload()]), format="json")
        self.assertEqual(free.data["conflicts"], [])
