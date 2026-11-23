"""
Tests for ``result``/``resultLabel`` in defense payloads and ``viewerMembership`` on the detail.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from adjudication.consumer import handle_event as handle_adjudication_event
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, DefenseMember, DefenseResult, DefenseStatus, Thesis, ThesisStatus
from theses.services import record_defense_result
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
MEMBERSHIP_KEYS = {"isMember", "memberId", "role", "roleLabel", "canEvaluate", "hasEvaluated"}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseResultPayloadTests(TestCase):
    """result/resultLabel are null until COMPLETED and match the computed close result."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.evaluator = create_user("ev@ufvjm.edu.br", "Avaliador", ProfileRole.ADVISOR)
        self.alternate_user = create_user("alt@ufvjm.edu.br", "Suplente", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.member = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.evaluator, actor=self.admin)
        self.alternate = add_institutional_member(self.defense, CommitteeRole.ALTERNATE, self.alternate_user, actor=self.admin)
        invitation, _token = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        self.external_member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        self.external_principal = ExternalInvitationPrincipal(invitation)
        confirm_committee(self.defense)
        self.client = APIClient()

    def _detail(self, principal) -> dict:
        self.client.force_authenticate(user=principal)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.status_code, 200)
        return response.data["defense"]

    def _list_item(self, principal) -> dict:
        self.client.force_authenticate(user=principal)
        response = self.client.get("/api/v1/defenses")
        return next(item for item in response.data["results"] if item["id"] == str(self.defense.pk))

    def _drain_adjudication(self) -> None:
        """Process the queued evaluation events like the adjudication worker."""
        processed = set(ProcessedEvent.objects.filter(consumer_group="adjudication").values_list("event_id", flat=True))
        pending = OutboxEvent.objects.filter(event_type=EventType.DEFENSE_EVALUATION_RECORDED).exclude(pk__in=processed).order_by("created_at")
        for event in pending:
            handle_adjudication_event(EventEnvelope.from_outbox(event))

    def _evaluate(self, principal, member: DefenseMember, decision: str):
        self.client.force_authenticate(user=principal)
        response = self.client.put(f"/api/v1/defenses/{self.defense.pk}/members/{member.pk}/evaluation", {"decision": decision, "score": "8.00", "notes": "Parecer de teste."}, format="json")
        if response.status_code == 200:
            self._drain_adjudication()
        return response

    # --- result / resultLabel ---

    def test_result_is_null_before_completion(self) -> None:
        detail = self._detail(self.admin)
        self.assertIn("result", detail)
        self.assertIsNone(detail["result"])
        self.assertIsNone(detail["resultLabel"])
        start_defense(self.defense, actor=self.admin)
        self.assertIsNone(self._detail(self.admin)["result"])
        self.assertIsNone(self._list_item(self.admin)["result"])

    def test_auto_close_result_visible_to_evaluator_and_external(self) -> None:
        start_defense(self.defense, actor=self.admin)
        self._evaluate(self.evaluator, self.member, DefenseResult.APPROVED)
        response = self._evaluate(self.external_principal, self.external_member, DefenseResult.REJECTED)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.COMPLETED)

        evaluator_detail = self._detail(self.evaluator)
        self.assertEqual(evaluator_detail["result"], DefenseResult.REJECTED)
        self.assertEqual(evaluator_detail["resultLabel"], DefenseResult.REJECTED.label)
        self.assertEqual(self._list_item(self.evaluator)["result"], DefenseResult.REJECTED)

        external_detail = self._detail(self.external_principal)
        self.assertEqual(external_detail["result"], DefenseResult.REJECTED)
        self.assertEqual(external_detail["resultLabel"], DefenseResult.REJECTED.label)

        self.assertEqual(self._list_item(self.admin)["resultLabel"], DefenseResult.REJECTED.label)

    def test_manual_close_result(self) -> None:
        start_defense(self.defense, actor=self.admin)
        record_evaluation_without_auto_close(self.member, DefenseResult.APPROVED)
        record_evaluation_without_auto_close(self.external_member, DefenseResult.APPROVED_WITH_REMARKS)
        with patch("theses.defense_scheduling.purge_thesis_versions"):
            close_defense(self.defense, actor=self.advisor)
        self.assertEqual(self._detail(self.advisor)["result"], DefenseResult.APPROVED_WITH_REMARKS)
        self.assertEqual(self._list_item(self.advisor)["resultLabel"], DefenseResult.APPROVED_WITH_REMARKS.label)

    def test_manual_thesis_level_result_does_not_leak_into_other_defenses(self) -> None:
        # record_defense_result grava defenseResult sem defenseId; não pode virar o resultado de uma banca.
        start_defense(self.defense, actor=self.admin)
        record_defense_result(self.coordinator, self.thesis, DefenseResult.REJECTED)
        self.assertIsNone(self._detail(self.admin)["result"])

    def test_cancelled_defense_has_no_result(self) -> None:
        from theses.defense_scheduling import cancel_defense
        cancel_defense(self.defense, actor=self.admin)
        detail = self._detail(self.admin)
        self.assertEqual(detail["status"], DefenseStatus.CANCELLED)
        self.assertIsNone(detail["result"])

    # --- viewerMembership no detalhe ---

    def test_detail_membership_for_institutional_evaluator(self) -> None:
        membership = self._detail(self.evaluator)["viewerMembership"]
        self.assertEqual(set(membership), MEMBERSHIP_KEYS)
        self.assertTrue(membership["isMember"])
        self.assertEqual(membership["memberId"], str(self.member.pk))
        self.assertEqual(membership["role"], CommitteeRole.PRESIDENT)
        self.assertEqual(membership["roleLabel"], CommitteeRole.PRESIDENT.label)
        self.assertTrue(membership["canEvaluate"])
        self.assertFalse(membership["hasEvaluated"])
        start_defense(self.defense, actor=self.admin)
        self._evaluate(self.evaluator, self.member, DefenseResult.APPROVED)
        self.assertTrue(self._detail(self.evaluator)["viewerMembership"]["hasEvaluated"])

    def test_detail_membership_matches_listing(self) -> None:
        self.assertEqual(self._detail(self.evaluator)["viewerMembership"], self._list_item(self.evaluator)["viewerMembership"])
        self.assertEqual(self._detail(self.admin)["viewerMembership"], self._list_item(self.admin)["viewerMembership"])

    def test_detail_membership_for_alternate(self) -> None:
        membership = self._detail(self.alternate_user)["viewerMembership"]
        self.assertTrue(membership["isMember"])
        self.assertEqual(membership["role"], CommitteeRole.ALTERNATE)
        self.assertFalse(membership["canEvaluate"])

    def test_detail_membership_for_non_member(self) -> None:
        membership = self._detail(self.admin)["viewerMembership"]
        self.assertEqual(membership, {"isMember": False, "memberId": None, "role": None, "roleLabel": None, "canEvaluate": False, "hasEvaluated": False})

    def test_detail_membership_for_external_session(self) -> None:
        membership = self._detail(self.external_principal)["viewerMembership"]
        self.assertTrue(membership["isMember"])
        self.assertEqual(membership["memberId"], str(self.external_member.pk))
        self.assertEqual(membership["role"], CommitteeRole.MEMBER)
        self.assertTrue(membership["canEvaluate"])
        self.assertFalse(membership["hasEvaluated"])
        start_defense(self.defense, actor=self.admin)
        self._evaluate(self.external_principal, self.external_member, DefenseResult.APPROVED)
        self.assertTrue(self._detail(self.external_principal)["viewerMembership"]["hasEvaluated"])

    def test_existing_detail_fields_are_kept(self) -> None:
        detail = self._detail(self.admin)
        for key in ("id", "thesis", "scheduledDatetime", "durationMinutes", "modality", "modalityLabel", "location", "notes", "status", "statusLabel", "createdAt", "updatedAt", "members", "statusHistory", "allMembersConfirmed", "pendingConfirmations"):
            self.assertIn(key, detail)
