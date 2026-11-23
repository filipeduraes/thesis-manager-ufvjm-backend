"""
Tests for ``viewerPermissions`` and the availability privacy rules of defense payloads.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member, respond_availability
from theses.defense_scheduling import cancel_defense, schedule_defense, start_defense
from theses.models import AvailabilityStatus, CommitteeRole, Defense, DefenseMember, Thesis, ThesisStatus
from theses.tests.defense_helpers import confirm_committee

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PERMISSION_KEYS: tuple[str, ...] = ("canManage", "canEdit", "canCancel", "canStart", "canClose", "canRevert", "canManageMembers")
AVAILABILITY_KEYS: tuple[str, ...] = ("availabilityStatus", "availabilityStatusLabel", "availabilityRespondedAt", "availabilityNote")
NO_PERMISSIONS: dict[str, bool] = {key: False for key in PERMISSION_KEYS}
SCHEDULED_MANAGER_PERMISSIONS: dict[str, bool] = {"canManage": True, "canEdit": True, "canCancel": True, "canStart": True, "canClose": False, "canRevert": False, "canManageMembers": True}


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseViewerPermissionsTests(TestCase):
    """viewerPermissions follows _can_manage_defense and the status; availability is private to managers."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.course_coordinator = create_user("coord-a@ufvjm.edu.br", "Coord A", ProfileRole.COORDINATOR)
        self.other_coordinator = create_user("coord-b@ufvjm.edu.br", "Coord B", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.course_coordinator, name="CC", code="CC001")
        Course.objects.create(department=self.department, coordinator=self.other_coordinator, name="SI", code="SI001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.member_user = create_user("member@ufvjm.edu.br", "Prof. Membro", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.coordinator_member = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.other_coordinator, actor=self.admin)
        self.plain_member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        self.external_member, self.external_principal = self._add_external_member()
        respond_availability(self.defense, self.coordinator_member, availability_status=AvailabilityStatus.CONFIRMED, note="Coord confirma.", actor=self.other_coordinator)
        respond_availability(self.defense, self.plain_member, availability_status=AvailabilityStatus.DECLINED, note="Membro recusa.", actor=self.member_user)
        self.client = APIClient()

    def _add_external_member(self) -> tuple[DefenseMember, ExternalInvitationPrincipal]:
        invitation = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Dr. Externo", email="ext@example.com")
        return member, ExternalInvitationPrincipal(invitation)

    def _detail(self, principal: User | ExternalInvitationPrincipal) -> dict[str, Any]:
        self.client.force_authenticate(user=principal)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.status_code, 200)
        return response.data["defense"]

    def _listing_item(self, principal: User) -> dict[str, Any]:
        self.client.force_authenticate(user=principal)
        response = self.client.get("/api/v1/defenses")
        self.assertEqual(response.status_code, 200)
        items = [item for item in response.data["results"] if item["id"] == str(self.defense.pk)]
        self.assertEqual(len(items), 1)
        return items[0]

    def _members_by_id(self, payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {member["id"]: member for member in payload["members"]}

    def _assert_full_availability(self, payload: dict[str, Any]) -> None:
        members = self._members_by_id(payload)
        self.assertEqual(members[str(self.coordinator_member.pk)]["availabilityStatus"], AvailabilityStatus.CONFIRMED)
        self.assertEqual(members[str(self.coordinator_member.pk)]["availabilityNote"], "Coord confirma.")
        self.assertEqual(members[str(self.plain_member.pk)]["availabilityStatus"], AvailabilityStatus.DECLINED)
        self.assertEqual(members[str(self.plain_member.pk)]["availabilityStatusLabel"], AvailabilityStatus.DECLINED.label)
        self.assertIsNotNone(members[str(self.plain_member.pk)]["availabilityRespondedAt"])
        self.assertEqual(members[str(self.external_member.pk)]["availabilityStatus"], AvailabilityStatus.PENDING)

    def _assert_only_own_availability(self, payload: dict[str, Any], own_member: DefenseMember) -> None:
        for member_id, member in self._members_by_id(payload).items():
            if member_id == str(own_member.pk):
                self.assertIsNotNone(member["availabilityStatus"])
                self.assertIsNotNone(member["availabilityStatusLabel"])
                continue
            for key in AVAILABILITY_KEYS:
                self.assertIsNone(member[key], f"{key} of member {member['name']}")

    # --- coordenador de outro curso que integra a banca ---

    def test_other_course_coordinator_member_cannot_manage(self) -> None:
        for payload in (self._detail(self.other_coordinator), self._listing_item(self.other_coordinator)):
            self.assertEqual(payload["viewerPermissions"], NO_PERMISSIONS)
            self.assertEqual(payload["viewerMembership"]["memberId"], str(self.coordinator_member.pk))
            self._assert_only_own_availability(payload, self.coordinator_member)
        own_member = self._members_by_id(self._detail(self.other_coordinator))[str(self.coordinator_member.pk)]
        self.assertEqual(own_member["availabilityNote"], "Coord confirma.")

    def test_other_course_coordinator_member_gets_no_pending_confirmations(self) -> None:
        detail = self._detail(self.other_coordinator)
        self.assertIsNone(detail["pendingConfirmations"])
        self.assertFalse(detail["allMembersConfirmed"])

    # --- coordenador do curso, orientador da tese e admin ---

    def test_managers_get_full_permissions_and_availability(self) -> None:
        for manager in (self.course_coordinator, self.advisor, self.admin):
            for payload in (self._detail(manager), self._listing_item(manager)):
                self.assertEqual(payload["viewerPermissions"], SCHEDULED_MANAGER_PERMISSIONS, manager.email)
                self._assert_full_availability(payload)
            detail = self._detail(manager)
            pending_ids = [item["memberId"] for item in detail["pendingConfirmations"]]
            self.assertEqual(pending_ids, [str(self.plain_member.pk), str(self.external_member.pk)], manager.email)
            self.assertFalse(detail["allMembersConfirmed"])

    def test_in_progress_permissions_follow_status(self) -> None:
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        in_progress_permissions = {"canManage": True, "canEdit": False, "canCancel": False, "canStart": False, "canClose": True, "canRevert": False, "canManageMembers": True}
        self.assertEqual(self._detail(self.advisor)["viewerPermissions"], in_progress_permissions)
        self.assertEqual(self._detail(self.course_coordinator)["viewerPermissions"], in_progress_permissions)
        admin_permissions = dict(in_progress_permissions, canRevert=True)
        self.assertEqual(self._detail(self.admin)["viewerPermissions"], admin_permissions)
        self.assertEqual(self._listing_item(self.admin)["viewerPermissions"], admin_permissions)

    def test_cancelled_defense_only_allows_admin_revert(self) -> None:
        cancel_defense(self.defense, reason="Remarcar", actor=self.admin)
        cancelled_permissions = dict(NO_PERMISSIONS, canManage=True)
        self.assertEqual(self._detail(self.advisor)["viewerPermissions"], cancelled_permissions)
        self.assertEqual(self._detail(self.admin)["viewerPermissions"], dict(cancelled_permissions, canRevert=True))

    def test_all_members_confirmed_is_visible_to_everyone(self) -> None:
        confirm_committee(self.defense)
        for principal in (self.admin, self.other_coordinator, self.member_user, self.external_principal):
            detail = self._detail(principal)
            self.assertTrue(detail["allMembersConfirmed"])

    # --- membro comum e externo ---

    def test_common_member_sees_only_own_availability(self) -> None:
        for payload in (self._detail(self.member_user), self._listing_item(self.member_user)):
            self.assertEqual(payload["viewerPermissions"], NO_PERMISSIONS)
            self._assert_only_own_availability(payload, self.plain_member)
        detail = self._detail(self.member_user)
        own_member = self._members_by_id(detail)[str(self.plain_member.pk)]
        self.assertEqual(own_member["availabilityStatus"], AvailabilityStatus.DECLINED)
        self.assertEqual(own_member["availabilityNote"], "Membro recusa.")
        self.assertIsNone(detail["pendingConfirmations"])

    def test_external_session_has_no_permissions_and_only_own_availability(self) -> None:
        detail = self._detail(self.external_principal)
        self.assertEqual(detail["viewerPermissions"], NO_PERMISSIONS)
        self.assertEqual(detail["viewerMembership"]["memberId"], str(self.external_member.pk))
        self._assert_only_own_availability(detail, self.external_member)
        self.assertIsNone(detail["pendingConfirmations"])

    def test_thesis_defense_list_hides_availability_from_student(self) -> None:
        student = create_user("aluno@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        self.thesis.authors.create(author=student, is_main_author=True)
        self.client.force_authenticate(user=student)
        response = self.client.get(f"/api/v1/theses/{self.thesis.pk}/defenses")
        self.assertEqual(response.status_code, 200)
        for member in response.data["defenses"][0]["members"]:
            for key in AVAILABILITY_KEYS:
                self.assertIsNone(member[key])

    # --- consultas da listagem ---

    def _listing_query_count(self, principal: User) -> int:
        self.client.force_authenticate(user=principal)
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/defenses")
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def _schedule_extra_defense(self, index: int) -> Defense:
        thesis = Thesis.objects.create(course=self.course, advisor=self.advisor, title=f"Tese extra {index}", status=ThesisStatus.ADVISOR_APPROVED)
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=10 + index), actor=self.admin)
        add_institutional_member(defense, CommitteeRole.PRESIDENT, self.other_coordinator, actor=self.admin)
        add_institutional_member(defense, CommitteeRole.MEMBER, self.member_user, actor=self.admin)
        return defense

    def test_listing_query_count_does_not_grow_with_defenses(self) -> None:
        viewers: tuple[User, ...] = (self.admin, self.course_coordinator, self.advisor, self.other_coordinator, self.member_user)
        for viewer in viewers:
            self._listing_query_count(viewer)
        baseline: dict[str, int] = {viewer.email: self._listing_query_count(viewer) for viewer in viewers}
        for index in range(4):
            self._schedule_extra_defense(index)
        for viewer in viewers:
            self.client.force_authenticate(user=viewer)
            response = self.client.get("/api/v1/defenses")
            self.assertEqual(response.data["count"], 5, viewer.email)
            self.assertEqual(self._listing_query_count(viewer), baseline[viewer.email], viewer.email)
