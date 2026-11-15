"""
API tests for the defense listing and schedulable-theses endpoints.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import cancel_defense, schedule_defense
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseResult, DefenseStatus, Thesis, ThesisAuthor, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DEFENSES_URL = "/api/v1/defenses"
SCHEDULABLE_URL = "/api/v1/defenses/schedulable-theses"


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


class DefenseListingFixtureMixin:
    """Shared fixture: two theses with defenses and one schedulable thesis per advisor."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.member_professor = create_user("member@ufvjm.edu.br", "Membro", ProfileRole.ADVISOR)
        self.student = create_user("student@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)

        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese A", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author=self.student, is_main_author=True)
        self.second_thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese B", status=ThesisStatus.READY_FOR_DEFENSE)

        start = timezone.now() + timedelta(days=7)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=start, location="Sala 1", actor=self.admin)
        self.second_defense = schedule_defense(self.second_thesis, scheduled_datetime=start + timedelta(days=1), actor=self.admin)
        self.second_defense = cancel_defense(self.second_defense, actor=self.admin)

        self.member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_professor, actor=self.admin)
        self.client = APIClient()

    def _ids(self, response) -> set[str]:
        return {item["id"] for item in response.data["results"]}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseListApiTests(DefenseListingFixtureMixin, TestCase):
    """Tests for GET /api/v1/defenses."""

    def test_unauthenticated_returns_401(self) -> None:
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 401)

    def test_admin_sees_all_defenses(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {str(self.defense.pk), str(self.second_defense.pk)})
        self.assertEqual(response.data["count"], 2)

    def test_coordinator_sees_all_defenses(self) -> None:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {str(self.defense.pk), str(self.second_defense.pk)})

    def test_advisor_sees_defenses_of_own_theses(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {str(self.defense.pk), str(self.second_defense.pk)})

    def test_advisor_of_other_thesis_gets_empty_list(self) -> None:
        self.client.force_authenticate(user=self.other_advisor)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)
        self.assertEqual(response.data["results"], [])

    def test_committee_member_sees_only_participating_defenses(self) -> None:
        self.client.force_authenticate(user=self.member_professor)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {str(self.defense.pk)})
        membership = response.data["results"][0]["viewerMembership"]
        self.assertTrue(membership["isMember"])
        self.assertEqual(membership["memberId"], str(self.member.pk))
        self.assertEqual(membership["role"], CommitteeRole.MEMBER)
        self.assertTrue(membership["canEvaluate"])
        self.assertFalse(membership["hasEvaluated"])

    def test_committee_member_has_evaluated_flag(self) -> None:
        DefenseMemberEvaluation.objects.create(member=self.member, decision=DefenseResult.APPROVED)
        self.client.force_authenticate(user=self.member_professor)
        response = self.client.get(DEFENSES_URL)
        self.assertTrue(response.data["results"][0]["viewerMembership"]["hasEvaluated"])

    def test_external_member_cannot_access_institutional_list(self) -> None:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        self.client.force_authenticate(user=ExternalInvitationPrincipal(invitation))
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(response.status_code, 403)

    def test_status_filter(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL, {"status": "cancelled"})
        self.assertEqual(self._ids(response), {str(self.second_defense.pk)})
        response = self.client.get(DEFENSES_URL, {"status": "SCHEDULED,IN_PROGRESS"})
        self.assertEqual(self._ids(response), {str(self.defense.pk)})

    def test_unknown_status_filter_is_ignored(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL, {"status": "FOO"})
        self.assertEqual(response.data["count"], 2)

    def test_payload_contract(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL, {"status": "SCHEDULED"})
        item = response.data["results"][0]
        self.assertEqual(item["thesis"], str(self.thesis.pk))
        self.assertEqual(item["thesisTitle"], "Tese A")
        self.assertEqual(item["thesisReferenceCode"], self.thesis.reference_code)
        self.assertEqual(item["student"]["id"], str(self.student.pk))
        self.assertEqual(item["advisor"]["id"], str(self.advisor.pk))
        self.assertEqual(item["durationMinutes"], 120)
        self.assertEqual(item["modality"], "IN_PERSON")
        self.assertEqual(item["location"], "Sala 1")
        self.assertEqual(item["status"], DefenseStatus.SCHEDULED)
        self.assertEqual(item["statusLabel"], DefenseStatus.SCHEDULED.label)
        self.assertIn("scheduledDatetime", item)
        self.assertEqual(len(item["members"]), 1)
        self.assertFalse(item["viewerMembership"]["isMember"])
        self.assertIsNone(item["viewerMembership"]["memberId"])
        for key in ("page", "pageSize", "totalPages", "next", "previous"):
            self.assertIn(key, response.data)

    def test_thesis_without_student_returns_null(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL, {"status": "CANCELLED"})
        self.assertIsNone(response.data["results"][0]["student"])

    def test_student_sees_own_thesis_defense(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.get(DEFENSES_URL)
        self.assertEqual(self._ids(response), {str(self.defense.pk)})

    def test_overdue_defense_is_auto_started_before_filtering(self) -> None:
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(minutes=5))
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(DEFENSES_URL, {"status": "IN_PROGRESS"})
        self.assertEqual(self._ids(response), {str(self.defense.pk)})
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.IN_PROGRESS)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class SchedulableThesisListApiTests(DefenseListingFixtureMixin, TestCase):
    """Tests for GET /api/v1/defenses/schedulable-theses."""

    def setUp(self) -> None:
        super().setUp()
        # Tese B teve a banca cancelada → volta a ser agendável. Tese A tem banca ativa.
        self.other_thesis = Thesis.objects.create(course=self.course, advisor=self.other_advisor, title="Tese C", status=ThesisStatus.ADVISOR_APPROVED)
        Thesis.objects.create(course=self.course, advisor=self.advisor, title="Rascunho", status=ThesisStatus.DRAFT)

    def test_unauthenticated_returns_401(self) -> None:
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(response.status_code, 401)

    def test_admin_sees_all_schedulable(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._ids(response), {str(self.second_thesis.pk), str(self.other_thesis.pk)})

    def test_coordinator_sees_all_schedulable(self) -> None:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(self._ids(response), {str(self.second_thesis.pk), str(self.other_thesis.pk)})

    def test_advisor_sees_only_own(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(self._ids(response), {str(self.second_thesis.pk)})

    def test_advisor_without_schedulable_theses_gets_empty_list(self) -> None:
        self.client.force_authenticate(user=self.member_professor)
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_student_gets_empty_list(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["results"], [])

    def test_external_member_is_forbidden(self) -> None:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        self.client.force_authenticate(user=ExternalInvitationPrincipal(invitation))
        response = self.client.get(SCHEDULABLE_URL)
        self.assertEqual(response.status_code, 403)

    def test_payload_contract(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.get(SCHEDULABLE_URL)
        item = response.data["results"][0]
        self.assertEqual(item["referenceCode"], self.second_thesis.reference_code)
        self.assertEqual(item["status"], ThesisStatus.READY_FOR_DEFENSE)
        self.assertIn("student", item)
        self.assertIn("advisor", item)
        self.assertIn("course", item)
