"""
Service-layer tests for defense scheduling, committee management, and evaluation.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from academic.models import Course, Department
from accounts.models import AuditLog, ProfileRole, User, UserProfileRole
from theses.defense_committee import _can_manage_defense, add_institutional_member, remove_member
from theses.defense_evaluation import submit_committee_evaluation
from theses.defense_scheduling import cancel_defense, close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, Defense, DefenseMember, DefenseMemberEvaluation, DefenseModality, DefenseResult, DefenseStatus, Thesis, ThesisStatus, ThesisStatusHistory
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES)
class DefenseSchedulingServiceTests(TestCase):
    """Tests for schedule_defense, cancel_defense, start_defense, and close_defense."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenador", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.student = create_user("student@ufvjm.edu.br", "Estudante", ProfileRole.STUDENT)

    def _make_thesis(self, status: str = ThesisStatus.ADVISOR_APPROVED) -> Thesis:
        thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese Teste", status=status)
        return thesis

    def _make_scheduled_defense(self, thesis: Thesis) -> Defense:
        return schedule_defense(
            thesis,
            scheduled_datetime=timezone.now() + timedelta(days=7),
            actor=self.admin,
        )

    # --- schedule_defense ---

    def test_admin_can_schedule(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        self.assertEqual(defense.status, DefenseStatus.SCHEDULED)
        thesis.refresh_from_db()
        self.assertEqual(thesis.status, ThesisStatus.DEFENSE_SCHEDULED)

    def test_coordinator_can_schedule(self) -> None:
        thesis = self._make_thesis()
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.coordinator)
        self.assertEqual(defense.status, DefenseStatus.SCHEDULED)

    def test_advisor_own_thesis_can_schedule(self) -> None:
        thesis = self._make_thesis()
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.advisor)
        self.assertEqual(defense.status, DefenseStatus.SCHEDULED)

    def test_advisor_other_thesis_raises_permission_denied(self) -> None:
        thesis = self._make_thesis()
        with self.assertRaises(PermissionDenied):
            schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.other_advisor)

    def test_student_cannot_schedule(self) -> None:
        thesis = self._make_thesis()
        with self.assertRaises(PermissionDenied):
            schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.student)

    def test_schedule_invalid_status_raises(self) -> None:
        thesis = self._make_thesis(status=ThesisStatus.DRAFT)
        with self.assertRaises(ValidationError):
            self._make_scheduled_defense(thesis)

    def test_schedule_records_status_history(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        entry = ThesisStatusHistory.objects.filter(thesis=thesis, new_status=ThesisStatus.DEFENSE_SCHEDULED).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.previous_status, ThesisStatus.ADVISOR_APPROVED)
        self.assertEqual(str(defense.pk), entry.metadata.get("defenseId"))

    # --- cancel_defense ---

    def test_cancel_restores_thesis_status(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        cancel_defense(defense, actor=self.admin)
        defense.refresh_from_db()
        thesis.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.CANCELLED)
        self.assertEqual(thesis.status, ThesisStatus.ADVISOR_APPROVED)

    def test_cancel_in_progress_raises(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        start_defense(defense, actor=self.admin)
        defense.refresh_from_db()
        with self.assertRaises(ValidationError):
            cancel_defense(defense, actor=self.admin)

    def test_advisor_other_thesis_cannot_cancel(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        with self.assertRaises(PermissionDenied):
            cancel_defense(defense, actor=self.other_advisor)

    # --- start_defense ---

    def test_start_transitions_to_in_progress(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        start_defense(defense, actor=self.admin)
        defense.refresh_from_db()
        thesis.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.IN_PROGRESS)
        self.assertEqual(thesis.status, ThesisStatus.UNDER_EVALUATION)

    def test_advisor_of_own_thesis_can_start_defense(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        start_defense(defense, actor=self.advisor)
        defense.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.IN_PROGRESS)

    def test_advisor_other_thesis_cannot_start_defense(self) -> None:
        thesis = self._make_thesis()
        defense = self._make_scheduled_defense(thesis)
        with self.assertRaises(PermissionDenied):
            start_defense(defense, actor=self.other_advisor)


@override_settings(CACHES=TEST_CACHES)
class CommitteeMemberServiceTests(TestCase):
    """Tests for add_institutional_member, remove_member, and schedule-overlap logic."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenador", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.other_advisor = create_user("other@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.member_user = create_user("member@ufvjm.edu.br", "Membro", ProfileRole.ADVISOR)

        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)

    # --- overlap ---

    def test_overlap_raises_validation_error(self) -> None:
        # Primeira banca: começa daqui 7 dias, 120 min
        t1 = timezone.now() + timedelta(days=7)
        add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)

        # Segunda tese com banca que se sobrepõe (mesma hora)
        thesis2 = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese 2", status=ThesisStatus.ADVISOR_APPROVED)
        defense2 = schedule_defense(thesis2, scheduled_datetime=t1 + timedelta(minutes=60), actor=self.admin)

        with self.assertRaises(ValidationError):
            add_institutional_member(defense2, CommitteeRole.MEMBER, self.member_user, actor=self.admin)

    def test_adjacent_session_does_not_raise(self) -> None:
        # Banca 1: T a T+120. Banca 2: T+120 a T+240. Adjacentes, não sobrepõem.
        t1 = timezone.now() + timedelta(days=7)
        add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)

        thesis2 = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese 2", status=ThesisStatus.ADVISOR_APPROVED)
        defense2 = schedule_defense(thesis2, scheduled_datetime=t1 + timedelta(minutes=120), actor=self.admin)

        # Não deve levantar ValidationError
        add_institutional_member(defense2, CommitteeRole.MEMBER, self.member_user, actor=self.admin)

    # --- remove_member ---

    def test_remove_blocked_in_progress(self) -> None:
        member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()
        with self.assertRaises(ValidationError):
            remove_member(self.defense, member, actor=self.admin)

    def test_remove_blocked_if_has_evaluation(self) -> None:
        member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        # Forçar avaliação diretamente (sem iniciar defesa, apenas para testar a guarda)
        DefenseMemberEvaluation.objects.create(member=member, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.")
        with self.assertRaises(ValidationError):
            remove_member(self.defense, member, actor=self.admin)

    def test_advisor_other_thesis_cannot_add_member(self) -> None:
        with self.assertRaises(PermissionDenied):
            add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.other_advisor)

    # --- _can_manage_defense ---

    def test_can_manage_own_thesis(self) -> None:
        self.assertTrue(_can_manage_defense(self.advisor, self.thesis.pk))

    def test_cannot_manage_other_thesis(self) -> None:
        self.assertFalse(_can_manage_defense(self.other_advisor, self.thesis.pk))

    def test_admin_can_manage_any(self) -> None:
        self.assertTrue(_can_manage_defense(self.admin, self.thesis.pk))


@override_settings(CACHES=TEST_CACHES)
class EvaluationServiceTests(TestCase):
    """Tests for submit_committee_evaluation."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenador", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.evaluator1 = create_user("ev1@ufvjm.edu.br", "Avaliador 1", ProfileRole.ADVISOR)
        self.evaluator2 = create_user("ev2@ufvjm.edu.br", "Avaliador 2", ProfileRole.ADVISOR)
        self.alternate_user = create_user("alt@ufvjm.edu.br", "Suplente", ProfileRole.ADVISOR)
        self.other_user = create_user("other@ufvjm.edu.br", "Outro", ProfileRole.ADVISOR)

        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.member1 = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.evaluator1, actor=self.admin)
        self.member2 = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.evaluator2, actor=self.admin)
        self.alternate = add_institutional_member(self.defense, CommitteeRole.ALTERNATE, self.alternate_user, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()

    # --- evaluation blocks ---

    def test_alternate_cannot_evaluate(self) -> None:
        with self.assertRaises(PermissionDenied):
            submit_committee_evaluation(self.defense, self.alternate, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.alternate_user)

    def test_cannot_evaluate_as_other_member(self) -> None:
        with self.assertRaises(ValidationError):
            submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator2)

    def test_score_above_10_raises(self) -> None:
        with self.assertRaises(ValidationError):
            submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="10.01", notes="Parecer de teste.", actor=self.evaluator1)

    def test_score_below_0_raises(self) -> None:
        with self.assertRaises(ValidationError):
            submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="-0.01", notes="Parecer de teste.", actor=self.evaluator1)

    def test_valid_evaluation_creates_record(self) -> None:
        ev = submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.5", notes="Parecer de teste.", actor=self.evaluator1)
        self.assertEqual(ev.decision, DefenseResult.APPROVED)
        self.assertIsNotNone(ev.score)

    def test_evaluation_update_or_create(self) -> None:
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        self.assertEqual(DefenseMemberEvaluation.objects.filter(member=self.member1).count(), 1)
        self.assertEqual(DefenseMemberEvaluation.objects.get(member=self.member1).decision, DefenseResult.REJECTED)

    # --- close_defense ---

    def test_close_defense_missing_evaluations_raises(self) -> None:
        # Apenas member1 avalia, member2 não
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        with self.assertRaises(ValidationError):
            close_defense(self.defense, actor=self.admin)

    def test_close_defense_result_rejected(self) -> None:
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        record_evaluation_without_auto_close(self.member2, DefenseResult.APPROVED)
        close_defense(self.defense, actor=self.admin)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.REJECTED)

    def test_close_defense_result_approved_with_remarks(self) -> None:
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        submit_committee_evaluation(self.defense, self.member2, decision=DefenseResult.APPROVED_WITH_REMARKS, score="8.00", notes="Parecer de teste.", actor=self.evaluator2)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(self.defense, actor=self.admin)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED_WITH_REMARKS)

    def test_close_defense_result_approved(self) -> None:
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        submit_committee_evaluation(self.defense, self.member2, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator2)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(self.defense, actor=self.admin)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED)

    def test_alternate_missing_evaluation_does_not_block_close(self) -> None:
        # Suplente não avaliou — não deve bloquear o encerramento
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        record_evaluation_without_auto_close(self.member2, DefenseResult.REJECTED)
        close_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.COMPLETED)

    def test_evaluation_blocked_after_completed_raises_and_audits(self) -> None:
        submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.REJECTED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)
        record_evaluation_without_auto_close(self.member2, DefenseResult.REJECTED)
        close_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()

        with self.assertRaises(ValidationError):
            submit_committee_evaluation(self.defense, self.member1, decision=DefenseResult.APPROVED, score="8.00", notes="Parecer de teste.", actor=self.evaluator1)

        # Verificar que a tentativa foi registrada no audit log
        blocked_entry = AuditLog.objects.filter(
            entity_id=str(self.defense.pk),
        ).order_by("-occurred_at").first()
        self.assertIsNotNone(blocked_entry)
        self.assertEqual(blocked_entry.metadata.get("event"), "EVALUATION_BLOCKED_AFTER_CLOSE")
