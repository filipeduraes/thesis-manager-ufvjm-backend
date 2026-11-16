"""
API tests for the defense scheduling, committee, and evaluation endpoints.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import AuditLog, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseResult, DefenseStatus, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseAuthTests(TestCase):
    """Tests for authentication and basic authorization on defense endpoints."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.student = create_user("student@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        self.other_student = create_user("other_student@ufvjm.edu.br", "Outro Aluno", ProfileRole.STUDENT)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.client = APIClient()

    def _make_defense(self) -> Defense:
        return schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)

    # --- 401 without authentication ---

    def test_unauthenticated_list_returns_401(self) -> None:
        response = self.client.get(f"/api/v1/theses/{self.thesis.pk}/defenses")
        self.assertEqual(response.status_code, 401)

    def test_unauthenticated_detail_returns_401(self) -> None:
        defense = self._make_defense()
        response = self.client.get(f"/api/v1/defenses/{defense.pk}")
        self.assertEqual(response.status_code, 401)

    # --- 403 student trying to schedule ---

    def test_student_cannot_schedule_defense(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", {"scheduledDatetime": (timezone.now() + timedelta(days=7)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 403)

    # --- 404 advisor of other thesis ---

    def test_other_advisor_cannot_see_defense_detail(self) -> None:
        defense = self._make_defense()
        self.client.force_authenticate(user=self.other_advisor)
        response = self.client.get(f"/api/v1/defenses/{defense.pk}")
        self.assertEqual(response.status_code, 404)

    def test_other_advisor_cannot_schedule_on_thesis(self) -> None:
        self.client.force_authenticate(user=self.other_advisor)
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", {"scheduledDatetime": (timezone.now() + timedelta(days=7)).isoformat()}, format="json")
        self.assertEqual(response.status_code, 404)

    # --- 404 student accessing another student's defense ---

    def test_other_student_cannot_see_defense(self) -> None:
        defense = self._make_defense()
        self.client.force_authenticate(user=self.other_student)
        response = self.client.get(f"/api/v1/defenses/{defense.pk}")
        self.assertEqual(response.status_code, 404)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseSchedulingApiTests(TestCase):
    """Tests for schedule, update, cancel, start, and close endpoints."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.client = APIClient()

    def test_advisor_schedules_own_thesis(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        payload = {"scheduledDatetime": (timezone.now() + timedelta(days=7)).isoformat(), "modality": "IN_PERSON", "location": "Sala 101"}
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", payload, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.SCHEDULED)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.DEFENSE_SCHEDULED)

    def test_admin_schedules_any_thesis(self) -> None:
        self.client.force_authenticate(user=self.admin)
        payload = {"scheduledDatetime": (timezone.now() + timedelta(days=7)).isoformat()}
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", payload, format="json")
        self.assertEqual(response.status_code, 201)

    def test_schedule_invalid_status_returns_400(self) -> None:
        self.thesis.status = ThesisStatus.DRAFT
        self.thesis.save()
        self.client.force_authenticate(user=self.admin)
        payload = {"scheduledDatetime": (timezone.now() + timedelta(days=7)).isoformat()}
        response = self.client.post(f"/api/v1/theses/{self.thesis.pk}/defenses", payload, format="json")
        self.assertEqual(response.status_code, 400)

    def test_patch_updates_location(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(f"/api/v1/defenses/{defense.pk}", {"location": "Sala 202"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["location"], "Sala 202")

    def test_cancel_restores_thesis_status(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(f"/api/v1/defenses/{defense.pk}/cancel", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.CANCELLED)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.ADVISOR_APPROVED)

    def test_start_transitions_defense_to_in_progress(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(f"/api/v1/defenses/{defense.pk}/start", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)

    def test_advisor_of_own_thesis_can_start_defense(self) -> None:
        defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.client.force_authenticate(user=self.advisor)
        response = self.client.post(f"/api/v1/defenses/{defense.pk}/start", {}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseEvaluationApiTests(TestCase):
    """Tests for committee evaluation submission and visibility."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.evaluator1 = create_user("ev1@ufvjm.edu.br", "Avaliador 1", ProfileRole.ADVISOR)
        self.evaluator2 = create_user("ev2@ufvjm.edu.br", "Avaliador 2", ProfileRole.ADVISOR)
        self.alternate_user = create_user("alt@ufvjm.edu.br", "Suplente", ProfileRole.ADVISOR)

        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.member1 = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.evaluator1, actor=self.admin)
        self.member2 = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.evaluator2, actor=self.admin)
        self.alternate = add_institutional_member(self.defense, CommitteeRole.ALTERNATE, self.alternate_user, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()
        self.client = APIClient()

    def _eval_url(self, member) -> str:
        return f"/api/v1/defenses/{self.defense.pk}/members/{member.pk}/evaluation"

    # --- ALTERNATE receives 403 ---

    def test_alternate_cannot_evaluate_returns_403(self) -> None:
        self.client.force_authenticate(user=self.alternate_user)
        response = self.client.put(self._eval_url(self.alternate), {"decision": DefenseResult.APPROVED}, format="json")
        self.assertEqual(response.status_code, 403)

    # --- Score validation ---

    def test_score_above_10_returns_400(self) -> None:
        self.client.force_authenticate(user=self.evaluator1)
        response = self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED, "score": "10.01"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_score_below_0_returns_400(self) -> None:
        self.client.force_authenticate(user=self.evaluator1)
        response = self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED, "score": "-0.01"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_valid_score_accepted(self) -> None:
        self.client.force_authenticate(user=self.evaluator1)
        response = self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED, "score": "9.5"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["evaluation"]["score"], "9.50")

    # --- Member A does not see member B's score ---

    def test_member_b_does_not_see_member_a_score(self) -> None:
        # Member 1 avalia com nota
        self.client.force_authenticate(user=self.evaluator1)
        self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED, "score": "9.0", "notes": "Excelente."}, format="json")

        # Member 2 acessa a avaliação de member 1
        self.client.force_authenticate(user=self.evaluator2)
        response = self.client.get(self._eval_url(self.member1))
        self.assertEqual(response.status_code, 200)
        # Score e notes NÃO devem estar presentes para outro membro
        eval_data = response.data["evaluation"]
        self.assertNotIn("score", eval_data)
        self.assertNotIn("notes", eval_data)

    # --- External invitation member evaluates ---

    def test_valid_external_member_can_evaluate(self) -> None:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        # Marcar convite como aceito para sessão válida
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])

        external_member = DefenseMember.objects.create(
            defense=self.defense,
            role=CommitteeRole.MEMBER,
            external_invitation=invitation,
            name="Dr. Externo",
            email="ext@example.com",
        )

        principal = ExternalInvitationPrincipal(invitation)
        self.client.force_authenticate(user=principal)
        response = self.client.put(self._eval_url(external_member), {"decision": DefenseResult.APPROVED}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_revoked_external_invitation_cannot_evaluate(self) -> None:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext2@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.REVOKED
        invitation.revoked_at = timezone.now()
        invitation.save(update_fields=["status", "revoked_at"])

        external_member = DefenseMember.objects.create(
            defense=self.defense,
            role=CommitteeRole.MEMBER,
            external_invitation=invitation,
            name="Dr. Externo",
            email="ext2@example.com",
        )

        principal = ExternalInvitationPrincipal(invitation)
        self.client.force_authenticate(user=principal)
        response = self.client.put(self._eval_url(external_member), {"decision": DefenseResult.APPROVED}, format="json")
        # IsDefenseCommitteeMember rejeita convite revogado → 403
        self.assertEqual(response.status_code, 403)

    # --- Evaluation after COMPLETED is blocked + audited ---

    def test_evaluation_after_completed_returns_400_and_audits(self) -> None:
        # Ambos avaliam com REJECTED (sem purge) e encerram
        from theses.defense_evaluation import submit_committee_evaluation
        from theses.defense_scheduling import close_defense

        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.REJECTED, actor=self.evaluator1)
        submit_committee_evaluation(self.defense, self.member2, decision=DefenseResult.REJECTED, actor=self.evaluator2)
        close_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.COMPLETED)

        # Tentar avaliar novamente via API
        self.client.force_authenticate(user=self.evaluator1)
        response = self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED}, format="json")
        self.assertEqual(response.status_code, 400)

        # Verificar entrada no audit log
        blocked = AuditLog.objects.filter(entity_id=str(self.defense.pk)).order_by("-occurred_at").first()
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked.metadata.get("event"), "EVALUATION_BLOCKED_AFTER_CLOSE")

    # --- Non-member institutional user blocked ---

    def test_non_member_coordinator_cannot_read_evaluation_returns_403(self) -> None:
        self.client.force_authenticate(user=self.evaluator1)
        self.client.put(self._eval_url(self.member1), {"decision": DefenseResult.APPROVED, "score": "9.0"}, format="json")

        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get(self._eval_url(self.member1))
        self.assertEqual(response.status_code, 403)
