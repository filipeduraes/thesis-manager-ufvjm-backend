"""
Tests for student groups of up to five authors in the submission flow.
"""

from __future__ import annotations

from rest_framework.test import APIClient

from accounts.models import ProfileRole
from theses.models import Thesis, ThesisAuthor
from theses.tests.repository_helpers import RepositoryTestCase
from theses.tests.test_api import create_user

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class AuthorGroupTests(RepositoryTestCase):
    """A draft accepts co-authors and stays inside the group size limit."""

    def setUp(self) -> None:
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(user=self.student)
        self.payload = {
            "title": "Monografia em grupo",
            "abstract": "Resumo do trabalho em grupo.",
            "courseId": str(self.course.pk),
            "advisorId": str(self.advisor.pk),
            "keywords": ["grupo"],
        }

    def save(self, authors: list[str]) -> object:
        """Send the draft payload with the given co-author identifiers."""
        return self.client.put("/api/v1/theses/me", {**self.payload, "authors": authors}, format="json")

    def test_group_of_five_is_accepted(self) -> None:
        coauthors = [create_user(f"co{i}@ufvjm.edu.br", f"Aluno {i}", ProfileRole.STUDENT) for i in range(4)]
        response = self.save([str(user.pk) for user in coauthors])
        self.assertEqual(response.status_code, 200, response.content)
        thesis = Thesis.objects.get(pk=response.data["thesis"]["id"])
        self.assertEqual(thesis.authors.count(), 5)
        self.assertEqual([link.author.name for link in thesis.authors.order_by("position")], ["Carla Estudante"] + [user.name for user in coauthors])
        self.assertTrue(thesis.authors.filter(is_main_author=True, author=self.student).exists())

    def test_sixth_student_is_refused(self) -> None:
        coauthors = [create_user(f"co{i}@ufvjm.edu.br", f"Aluno {i}", ProfileRole.STUDENT) for i in range(5)]
        response = self.save([str(user.pk) for user in coauthors])
        self.assertEqual(response.status_code, 400)
        self.assertFalse(Thesis.objects.exists())

    def test_advisor_cannot_be_coauthor(self) -> None:
        response = self.save([str(self.advisor.pk)])
        self.assertEqual(response.status_code, 400)

    def test_coauthor_can_see_and_update_the_draft(self) -> None:
        coauthor = create_user("co@ufvjm.edu.br", "Coautor", ProfileRole.STUDENT)
        self.assertEqual(self.save([str(coauthor.pk)]).status_code, 200)
        thesis = Thesis.objects.get()
        self.assertTrue(thesis.authors.filter(author=coauthor).exists())
        self.client.force_authenticate(user=coauthor)
        response = self.client.get(f"/api/v1/theses/{thesis.pk}")
        self.assertEqual(response.status_code, 200)
        response = self.save([str(coauthor.pk)])
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ThesisAuthor.objects.filter(thesis=thesis, author=self.student, is_main_author=True).exists())
        self.assertEqual(self.save([]).status_code, 400)
