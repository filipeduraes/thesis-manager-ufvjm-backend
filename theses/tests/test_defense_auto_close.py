"""
Tests for the automatic defense close after the last required evaluation.
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
from theses.defense_scheduling import AUTO_CLOSE_REASON, close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseResult, DefenseStatus, DefenseStatusHistory, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
NO_PDF_MESSAGE = "A monografia não possui versão atual."


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseAutoCloseTests(TestCase):
    """The last evaluation closes the defense through close_defense's logic, without ever losing the evaluation."""

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
        self.client = APIClient()

    def _evaluate(self, principal, member: DefenseMember, decision: str):
        self.client.force_authenticate(user=principal)
        return self.client.put(f"/api/v1/defenses/{self.defense.pk}/members/{member.pk}/evaluation", {"decision": decision}, format="json")

    def _defense_status(self) -> str:
        return Defense.objects.get(pk=self.defense.pk).status

    def test_intermediate_evaluation_keeps_defense_in_progress(self) -> None:
        response = self._evaluate(self.evaluator1, self.member1, DefenseResult.APPROVED)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["autoCloseError"])
        self.assertEqual(response.data["defenseStatus"], DefenseStatus.IN_PROGRESS)
        self.assertEqual(response.data["evaluation"]["decision"], DefenseResult.APPROVED)

    def test_last_evaluation_closes_defense_automatically(self) -> None:
        self._evaluate(self.evaluator1, self.member1, DefenseResult.APPROVED)
        response = self._evaluate(self.evaluator2, self.member2, DefenseResult.REJECTED)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["autoCloseError"])
        self.assertEqual(response.data["defenseStatus"], DefenseStatus.COMPLETED)
        self.assertEqual(self._defense_status(), DefenseStatus.COMPLETED)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.REJECTED)
        entry = DefenseStatusHistory.objects.filter(defense=self.defense).order_by("created_at").last()
        self.assertEqual((entry.previous_status, entry.new_status), (DefenseStatus.IN_PROGRESS, DefenseStatus.COMPLETED))
        self.assertIsNone(entry.changed_by)
        self.assertEqual(entry.reason, "Resultado calculado automaticamente após a última avaliação.")
        self.assertEqual(entry.reason, AUTO_CLOSE_REASON)
        closed = AuditLog.objects.filter(metadata__event="DEFENSE_CLOSED").get()
        self.assertEqual(closed.metadata["trigger"], "AUTO_AFTER_LAST_EVALUATION")

    def test_alternate_evaluation_is_not_required(self) -> None:
        self._evaluate(self.evaluator1, self.member1, DefenseResult.REJECTED)
        self._evaluate(self.evaluator2, self.member2, DefenseResult.REJECTED)
        self.assertFalse(DefenseMemberEvaluation.objects.filter(member=self.alternate).exists())
        self.assertEqual(self._defense_status(), DefenseStatus.COMPLETED)

    def test_approval_auto_close_purges_versions_like_manual_close(self) -> None:
        with patch("theses.defense_scheduling.purge_thesis_versions") as purge:
            self._evaluate(self.evaluator1, self.member1, DefenseResult.APPROVED)
            response = self._evaluate(self.evaluator2, self.member2, DefenseResult.APPROVED)
        self.assertEqual(response.data["defenseStatus"], DefenseStatus.COMPLETED)
        purge.assert_called_once()
        self.assertIsNone(purge.call_args.kwargs["actor"])
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED)

    def test_failed_auto_close_keeps_evaluation_and_reports_error(self) -> None:
        self._evaluate(self.evaluator1, self.member1, DefenseResult.APPROVED)
        with self.assertLogs("theses.defense_scheduling", level="WARNING") as captured:
            response = self._evaluate(self.evaluator2, self.member2, DefenseResult.APPROVED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["autoCloseError"], NO_PDF_MESSAGE)
        self.assertEqual(response.data["defenseStatus"], DefenseStatus.IN_PROGRESS)
        self.assertTrue(any("Automatic close failed" in line for line in captured.output))
        self.assertEqual(DefenseMemberEvaluation.objects.get(member=self.member2).decision, DefenseResult.APPROVED)
        self.assertEqual(self._defense_status(), DefenseStatus.IN_PROGRESS)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.UNDER_EVALUATION)
        self.assertFalse(DefenseStatusHistory.objects.filter(defense=self.defense, new_status=DefenseStatus.COMPLETED).exists())
        self.assertFalse(AuditLog.objects.filter(metadata__event="DEFENSE_CLOSED").exists())

    def test_manual_close_still_available_after_failed_auto_close(self) -> None:
        self._evaluate(self.evaluator1, self.member1, DefenseResult.APPROVED)
        self._evaluate(self.evaluator2, self.member2, DefenseResult.APPROVED)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(self.defense, actor=self.advisor)
        self.assertEqual(self._defense_status(), DefenseStatus.COMPLETED)
        entry = DefenseStatusHistory.objects.filter(defense=self.defense).order_by("created_at").last()
        self.assertEqual(entry.changed_by, self.advisor)
        self.assertEqual(entry.reason, f"Banca encerrada. Resultado: {DefenseResult.APPROVED.label}.")

    def test_no_evaluation_can_change_after_auto_close(self) -> None:
        self._evaluate(self.evaluator1, self.member1, DefenseResult.REJECTED)
        self._evaluate(self.evaluator2, self.member2, DefenseResult.REJECTED)
        for principal, member in ((self.evaluator1, self.member1), (self.evaluator2, self.member2), (self.alternate_user, self.alternate)):
            response = self._evaluate(principal, member, DefenseResult.APPROVED)
            self.assertEqual(response.status_code, 400)
        self.assertEqual(DefenseMemberEvaluation.objects.get(member=self.member1).decision, DefenseResult.REJECTED)
        self.assertEqual(DefenseMemberEvaluation.objects.get(member=self.member2).decision, DefenseResult.REJECTED)

    def test_external_member_last_evaluation_closes_defense(self) -> None:
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        external_member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        self._evaluate(self.evaluator1, self.member1, DefenseResult.REJECTED)
        self._evaluate(self.evaluator2, self.member2, DefenseResult.REJECTED)
        self.assertEqual(self._defense_status(), DefenseStatus.IN_PROGRESS)
        response = self._evaluate(ExternalInvitationPrincipal(invitation), external_member, DefenseResult.REJECTED)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defenseStatus"], DefenseStatus.COMPLETED)
