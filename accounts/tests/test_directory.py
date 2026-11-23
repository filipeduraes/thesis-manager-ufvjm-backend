"""
Tests for the public advisors list and the role-restricted directory.
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.request import Request
from rest_framework.test import APIClient, APIRequestFactory

from accounts.directory import DIRECTORY_MAX_PAGE_SIZE, _pagination_cache_keys

from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import suggest_users

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accounts-directory-test-cache",
    }
}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class UserDirectoryApiTests(TestCase):
    """Public advisors list and restricted directory behavior."""

    def setUp(self) -> None:
        cache.clear()
        self.client = APIClient()
        self.advisor = self._create_user("a@ufvjm.edu.br", "Orientadora Silva", ProfileRole.ADVISOR)
        self.advisor_b = self._create_user("b@ufvjm.edu.br", "Orientador Mendes", ProfileRole.ADVISOR)
        self.coordinator = self._create_user("c@ufvjm.edu.br", "Coordenador Roe", ProfileRole.COORDINATOR)
        self.admin = self._create_user("d@ufvjm.edu.br", "Administrador Souza", ProfileRole.ADMIN)
        self.student = self._create_user("e@ufvjm.edu.br", "Aluno Teste", ProfileRole.STUDENT)
        self.student_b = self._create_user("f@ufvjm.edu.br", "Aluna Biazini", ProfileRole.STUDENT)

    def _create_user(self, email: str, name: str, role: str):
        user = User.objects.create_user(email=email, password="senha123", name=name)
        UserProfileRole.objects.create(user=user, role=role)
        return user

    def test_pagination_cache_keys_are_normalized(self) -> None:
        request = Request(APIRequestFactory().get("/api/v1/public/advisors", {"page": "abc", "pageSize": "9999"}))
        self.assertEqual(_pagination_cache_keys(request), (1, DIRECTORY_MAX_PAGE_SIZE))

    # ─── Public advisors list ───────────────────────────────────────────────

    def test_public_advisors_list_is_anonymous_and_searchable(self) -> None:
        response = self.client.get("/api/v1/public/advisors")
        self.assertEqual(response.status_code, 200)
        for item in response.json()["results"]:
            self.assertNotIn("email", item, "public endpoint must not expose e-mail addresses")
        names = {item["name"] for item in response.json()["results"]}
        self.assertEqual(names, {"Orientadora Silva", "Orientador Mendes"})

        response = self.client.get("/api/v1/public/advisors", {"q": "silva"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({item["name"] for item in response.json()["results"]}, {"Orientadora Silva"})

    def test_public_advisors_list_honours_pagination(self) -> None:
        response = self.client.get("/api/v1/public/advisors", {"pageSize": "1", "page": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["results"]), 1)
        self.assertEqual(response.json()["count"], 2)
        page2 = self.client.get("/api/v1/public/advisors", {"pageSize": "1", "page": "2"})
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.json()["results"]), 1)

    # ─── Restricted directory ───────────────────────────────────────────────

    def test_directory_requires_coordinator_or_admin(self) -> None:
        self.client.force_authenticate(user=self.student)
        response = self.client.get("/api/v1/users", {"role": "STUDENT"})
        self.assertEqual(response.status_code, 403)

        self.client.force_authenticate(user=self.advisor)
        response = self.client.get("/api/v1/users", {"role": "STUDENT"})
        self.assertEqual(response.status_code, 403)

        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get("/api/v1/users", {"role": "STUDENT"})
        self.assertEqual(response.status_code, 200)

    def test_directory_role_filter_lists_only_requested_role(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/users", {"role": "STUDENT"})
        self.assertEqual({item["name"] for item in response.json()["results"]}, {"Aluno Teste", "Aluna Biazini"})
        response = self.client.get("/api/v1/users", {"role": "COORDINATOR"})
        self.assertEqual({item["name"] for item in response.json()["results"]}, {"Coordenador Roe"})
        response = self.client.get("/api/v1/users", {"role": "ADVISOR"})
        self.assertEqual({item["name"] for item in response.json()["results"]}, {"Orientadora Silva", "Orientador Mendes"})

    def test_directory_includes_email_and_pagination_keys(self) -> None:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get("/api/v1/users", {"role": "COORDINATOR"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"][0]["email"], "c@ufvjm.edu.br")
        for key in ("count", "page", "pageSize", "totalPages", "lastPageCount"):
            self.assertIn(key, response.json())

    # ─── Suggest restricts advisors to the advisors list ────────────────────

    def test_advisors_suggest_only_advisors_coordinators_search_all(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.get("/api/v1/users/suggest", {"q": "al"})
        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["results"]}
        self.assertEqual(names, set(), "advisors must only see other advisors")

        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get("/api/v1/users/suggest", {"q": "aluno"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aluno Teste", {item["name"] for item in response.json()["results"]})

    def test_coordinators_suggest_with_role_filter_returns_requested_role(self) -> None:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get("/api/v1/users/suggest", {"q": "a", "role": "STUDENT"})
        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["results"]}
        self.assertLessEqual(names, {"Aluno Teste", "Aluna Biazini"})
