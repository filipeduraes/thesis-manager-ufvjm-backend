"""
Integrity tests for overlapping operations.

These scenarios rely on application invariants and database constraints rather
than on PostgreSQL lock timing, so they also run on the default test database.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation, issue_external_token
from theses.defense_committee import add_institutional_member
from theses.defense_evaluation import submit_committee_evaluation
from theses.defense_scheduling import close_defense, schedule_defense, start_defense
from theses.models import CommitteeRole, DefenseMemberEvaluation, DefenseResult, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee, record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class CommitteeIntegrityTests(TestCase):
    """
    Committee invariants that overlapping requests must not violate.
    """

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(name="CC", code="CC001", department=self.department, coordinator=self.coordinator)
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Advisor", ProfileRole.ADVISOR)
        self.evaluator = create_user("ev@ufvjm.edu.br", "Avaliador", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="TCC", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)

    def test_same_institutional_member_cannot_be_added_twice(self) -> None:
        add_institutional_member(self.defense, CommitteeRole.MEMBER, self.evaluator, actor=self.admin)
        with self.assertRaises(ValidationError):
            add_institutional_member(self.defense, CommitteeRole.ALTERNATE, self.evaluator, actor=self.admin)
        self.assertEqual(self.defense.members.filter(institutional_member=self.evaluator).count(), 1)

    @patch("theses.defense_scheduling.purge_thesis_versions")
    def test_evaluation_with_stale_defense_is_rejected_after_close(self, _purge) -> None:
        member = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.evaluator, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        stale_defense = self.defense
        record_evaluation_without_auto_close(member, DefenseResult.APPROVED)
        close_defense(self.defense, actor=self.admin)

        with self.assertRaises(ValidationError):
            submit_committee_evaluation(
                defense=stale_defense,
                member=member,
                decision=DefenseResult.REJECTED,
                score="6.00",
                notes="Parecer de teste.",
                actor=self.evaluator,
            )
        self.assertFalse(
            DefenseMemberEvaluation.objects.filter(member=member, decision=DefenseResult.REJECTED).exists()
        )


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class ExternalInvitationIntegrityTests(TestCase):
    """
    Verify that EXPIRED is persisted even when token exchange fails.
    """

    def setUp(self) -> None:
        self.inviter = create_user("inviter@ufvjm.edu.br", "Convidante", ProfileRole.ADMIN)
        self.client = APIClient()

    def test_expired_invitation_status_is_persisted(self) -> None:
        invitation = create_external_invitation(
            name="Externo", email="externo@example.com", invited_by=self.inviter
        )
        # O token legível só existe no e-mail; o teste emite um diretamente, como o consumidor.
        token = issue_external_token(invitation)
        invitation.expires_at = timezone.now() - timedelta(hours=1)
        invitation.save(update_fields=["expires_at"])

        response = self.client.post("/api/v1/auth/external-token", {"token": token}, format="json")
        self.assertIn(response.status_code, (400, 401))
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.EXPIRED)
