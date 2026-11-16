"""
Tests for the user suggestion endpoint (accounts urls).
"""
from __future__ import annotations

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import ProfileRole, User, UserProfileRole
from accounts.services import suggest_users

TEST_CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "accounts-suggest-test-cache",
    }
}
SUGGEST_URL = "/api/v1/users/suggest"


def create_user(email: str, name: str, role: str | None = None, is_active: bool = True) -> User:
    """
    Create an active (or inactive) user, optionally with a role.

    Args:
        email: Institutional e-mail.
        name: Display name.
        role: Optional profile role code to assign.
        is_active: Whether the account stays active.

    Returns:
        User: The created user.
    """
    user = User.objects.create_user(email=email, password="senha123", name=name, is_active=is_active)
    if role:
        UserProfileRole.objects.create(user=user, role=role)
    return user


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class UserSuggestEndpointTests(TestCase):
    """Permission and contract tests for the suggestion endpoint."""

    def setUp(self) -> None:
        cache.clear()
        self.advisor = create_user("advisor@ufvjm.edu.br", "Orientadora Silva", ProfileRole.ADVISOR)
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenador Mendes", ProfileRole.COORDINATOR)
        self.admin = create_user("admin@ufvjm.edu.br", "Admin Souza", ProfileRole.ADMIN)
        self.student = create_user("aluno@ufvjm.edu.br", "Aluno Teste", ProfileRole.STUDENT)
        self.matched = create_user("lidia.silva@ufvjm.edu.br", "Lídia Silva", ProfileRole.ADVISOR)
        create_user("lidia.oliveira@ufvjm.edu.br", "Lídia Oliveira", ProfileRole.ADMIN)
        create_user("lidia.ferreira@ufvjm.edu.br", "Lídia Ferreira", ProfileRole.ADVISOR)
        create_user("lidia.retired@ufvjm.edu.br", "Lídia Retired", None, is_active=False)
        self.client = APIClient()

    def test_advisors_get_suggestions_students_do_not(self) -> None:
        self.client.force_authenticate(user=self.advisor)
        response = self.client.get(SUGGEST_URL, {"q": "lid"})
        self.assertEqual(response.status_code, 200)
        self.client.force_authenticate(user=self.student)
        denied = self.client.get(SUGGEST_URL, {"q": "lid"})
        self.assertEqual(denied.status_code, 403)

    def test_suggestion_matches_on_name_and_respects_limit(self) -> None:
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(SUGGEST_URL, {"q": "lídia"})
        self.assertEqual(response.status_code, 200)
        names = [item["name"] for item in response.json()["results"]]
        self.assertLessEqual(len(names), 3)
        self.assertNotIn("Lídia Retired", names)

    def test_short_queries_return_empty_results(self) -> None:
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.get(SUGGEST_URL, {"q": "l"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])

    def test_suggestions_are_cached_for_the_query(self) -> None:
        first = suggest_users("lídia")
        first_names = {item["name"] for item in first}
        User.objects.filter(pk=self.matched.pk).update(is_active=False)
        cached = suggest_users("lídia")
        # O valor servido é o que estava em cache na primeira chamada.
        self.assertEqual({item["name"] for item in cached}, first_names)
        cache.clear()
        refreshed = suggest_users("lídia")
        self.assertNotIn("Lídia Silva", {item["name"] for item in refreshed})
