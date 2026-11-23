"""
Tests for committee member avatars (Google profile picture) and the user lookup avatar.
"""

from __future__ import annotations

from datetime import timedelta

from allauth.socialaccount.models import SocialAccount
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense
from theses.models import CommitteeRole, DefenseMember, Thesis, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PICTURE_URL = "https://lh3.googleusercontent.com/a/membro-foto"


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, password="senha123", name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def link_google_account(user: User, picture: str | None) -> SocialAccount:
    extra_data = {"email": user.email}
    if picture is not None:
        extra_data["picture"] = picture
    return SocialAccount.objects.create(user=user, provider="google", uid=f"uid-{user.pk}", extra_data=extra_data)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class DefenseMemberAvatarTests(TestCase):
    """avatarUrl is the Google picture for institutional members and null otherwise."""

    def setUp(self) -> None:
        self.department = Department.objects.create(name="Computação")
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coord", ProfileRole.COORDINATOR)
        self.course = Course.objects.create(department=self.department, coordinator=self.coordinator, name="CC", code="CC001")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        self.google_user = create_user("google@ufvjm.edu.br", "Com Foto", ProfileRole.ADVISOR)
        self.plain_user = create_user("plain@ufvjm.edu.br", "Sem Conta", ProfileRole.ADVISOR)
        link_google_account(self.google_user, PICTURE_URL)
        self.thesis = Thesis.objects.create(course_id=self.course.pk, advisor_id=self.advisor.pk, title="Tese", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        self.google_member = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, self.google_user, actor=self.admin)
        self.plain_member = add_institutional_member(self.defense, CommitteeRole.MEMBER, self.plain_user, actor=self.admin)
        invitation = create_external_invitation(name="Dr. Externo", email="ext@example.com", institution="UFMG", invited_by=self.admin)
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        self.external_member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation_id=invitation.pk, name="Dr. Externo", email="ext@example.com")
        self.external_principal = ExternalInvitationPrincipal(invitation)
        self.client = APIClient()

    def _avatars_from(self, members: list[dict]) -> dict[str, str | None]:
        return {item["id"]: item["avatarUrl"] for item in members}

    def _assert_expected_avatars(self, members: list[dict]) -> None:
        avatars = self._avatars_from(members)
        self.assertEqual(avatars[str(self.google_member.pk)], PICTURE_URL)
        self.assertIsNone(avatars[str(self.plain_member.pk)])
        self.assertIsNone(avatars[str(self.external_member.pk)])

    def test_detail_member_avatars(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self._assert_expected_avatars(response.data["defense"]["members"])

    def test_listing_member_avatars(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/defenses")
        self._assert_expected_avatars(response.data["results"][0]["members"])

    def test_external_session_sees_member_avatars(self) -> None:
        self.client.force_authenticate(user=self.external_principal)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self._assert_expected_avatars(response.data["defense"]["members"])

    def test_google_account_without_safe_picture_returns_null(self) -> None:
        SocialAccount.objects.filter(user=self.google_user).update(extra_data={"picture": "http://insecure.example.com/p.png"})
        link_google_account(self.plain_user, None)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        avatars = self._avatars_from(response.data["defense"]["members"])
        self.assertIsNone(avatars[str(self.google_member.pk)])
        self.assertIsNone(avatars[str(self.plain_member.pk)])

    def test_member_add_response_includes_avatar(self) -> None:
        member_user = create_user("novo@ufvjm.edu.br", "Novo", ProfileRole.ADVISOR)
        link_google_account(member_user, PICTURE_URL)
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(f"/api/v1/defenses/{self.defense.pk}/members", {"role": CommitteeRole.ALTERNATE, "userId": str(member_user.pk)}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["member"]["avatarUrl"], PICTURE_URL)

    def _listing_query_count(self) -> int:
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get("/api/v1/defenses")
        self.assertEqual(response.status_code, 200)
        return len(captured.captured_queries)

    def test_listing_query_count_does_not_grow_with_members(self) -> None:
        for viewer in (self.admin, self.google_user):
            self.client.force_authenticate(user=viewer)
            self._listing_query_count()
        baseline = {}
        for viewer in (self.admin, self.google_user):
            self.client.force_authenticate(user=viewer)
            baseline[viewer.pk] = self._listing_query_count()
        for index in range(4):
            extra_user = create_user(f"extra{index}@ufvjm.edu.br", f"Extra {index}", ProfileRole.ADVISOR)
            link_google_account(extra_user, f"{PICTURE_URL}-{index}")
            add_institutional_member(self.defense, CommitteeRole.ALTERNATE, extra_user, actor=self.admin)
        for viewer in (self.admin, self.google_user):
            self.client.force_authenticate(user=viewer)
            self.assertEqual(self._listing_query_count(), baseline[viewer.pk], viewer.email)

    def test_detail_query_count_does_not_grow_with_members(self) -> None:
        self.client.force_authenticate(user=self.admin)
        url = f"/api/v1/defenses/{self.defense.pk}"
        self.client.get(url)
        with CaptureQueriesContext(connection) as before:
            self.client.get(url)
        for index in range(4):
            extra_user = create_user(f"extra{index}@ufvjm.edu.br", f"Extra {index}", ProfileRole.ADVISOR)
            link_google_account(extra_user, f"{PICTURE_URL}-{index}")
            add_institutional_member(self.defense, CommitteeRole.ALTERNATE, extra_user, actor=self.admin)
        with CaptureQueriesContext(connection) as after:
            response = self.client.get(url)
        self.assertEqual(len(response.data["defense"]["members"]), 7)
        self.assertEqual(len(after.captured_queries), len(before.captured_queries))


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db", ADMIN_ALLOWED_CIDRS=["127.0.0.1/32"])
class AdminUserLookupAvatarTests(TestCase):
    """GET /api/v1/admin/users/lookup adds avatarUrl without changing other fields."""

    def setUp(self) -> None:
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def _lookup(self, email: str):
        return self.client.get("/api/v1/admin/users/lookup", {"email": email}, REMOTE_ADDR="127.0.0.1")

    def test_lookup_returns_google_avatar(self) -> None:
        user = create_user("foto@ufvjm.edu.br", "Com Foto", ProfileRole.ADVISOR)
        link_google_account(user, PICTURE_URL)
        response = self._lookup("FOTO@ufvjm.edu.br")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["found"])
        payload = response.data["user"]
        self.assertEqual(payload["avatarUrl"], PICTURE_URL)
        for key in ("id", "name", "email", "isActive"):
            self.assertIn(key, payload)

    def test_lookup_without_google_account_returns_null_avatar(self) -> None:
        create_user("semfoto@ufvjm.edu.br", "Sem Foto", ProfileRole.ADVISOR)
        response = self._lookup("semfoto@ufvjm.edu.br")
        self.assertIsNone(response.data["user"]["avatarUrl"])
