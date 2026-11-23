"""
Tests of read access to the thesis by a committee member who must evaluate it.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import ProfileRole
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense, start_defense
from theses.models import CommitteeRole, ThesisStatus
from theses.tests.defense_helpers import confirm_committee
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class CommitteeThesisAccessTests(RepositoryTestCase):
    """A committee member reads the work without gaining management rights."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        self.evaluator = create_user("avaliador@ufvjm.edu.br", "Avaliador Convidado", ProfileRole.ADVISOR)
        self.thesis = create_thesis(ThesisSpec(title="Tese para avaliação", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["banca"], status=ThesisStatus.ADVISOR_APPROVED))
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        add_institutional_member(self.defense, CommitteeRole.MEMBER, self.evaluator, actor=self.admin)
        confirm_committee(self.defense)
        start_defense(self.defense, actor=self.admin)
        self.client = APIClient()

    def test_member_reads_detail_and_list(self) -> None:
        self.client.force_authenticate(user=self.evaluator)
        detail = self.client.get(f"/api/v1/theses/{self.thesis.pk}")
        self.assertEqual(detail.status_code, 200, detail.content)
        listing = self.client.get("/api/v1/theses")
        self.assertIn(str(self.thesis.pk), [item["id"] for item in listing.data["results"]])

    def test_member_downloads_the_current_pdf(self) -> None:
        file_id = self.thesis.files.get(is_current=True).pk
        self.client.force_authenticate(user=self.evaluator)
        response = self.client.get(f"/api/v1/theses/{self.thesis.pk}/files/{file_id}/download")
        self.assertEqual(response.status_code, 200)

    def test_non_member_advisor_has_no_read_access(self) -> None:
        other = create_user("outro@ufvjm.edu.br", "Outro Orientador", ProfileRole.ADVISOR)
        self.client.force_authenticate(user=other)
        self.assertEqual(self.client.get(f"/api/v1/theses/{self.thesis.pk}").status_code, 404)
