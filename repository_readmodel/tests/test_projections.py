"""
Tests of the repository read model projection and its public API.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from rest_framework.test import APIClient

from repository_readmodel.models import PublicThesis
from theses.repository_cache import current_cache_version
from repository_readmodel.projections import handle_event, project_thesis
from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import OutboxEvent, ProcessedEvent
from theses.events import record_thesis_published, record_thesis_repository_updated
from theses.models import ThesisStatus
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


@override_settings(CACHES=TEST_CACHES, ROOT_URLCONF="thesismanagerbackend.service_urls.repository")
class RepositoryProjectionTests(RepositoryTestCase):
    """The read model is rebuilt from events and serves anonymous searches."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()

    def test_publication_event_projects_thesis(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Tese projetada", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Projeção"], defense_date=date(2024, 6, 10)))
        with self._atomic_event():
            event = record_thesis_published(thesis)
        version_before = current_cache_version()
        outcome = handle_event(EventEnvelope.from_outbox(event))
        self.assertEqual(outcome, "PROCESSED")
        self.assertNotEqual(current_cache_version(), version_before)
        projected = PublicThesis.objects.get(pk=thesis.pk)
        self.assertEqual(projected.title, "Tese projetada")
        self.assertEqual(projected.keywords, ["Projeção"])
        self.assertEqual(projected.author_names, [self.student.name])

    def test_unsupported_event_version_is_rejected(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Versão futura", course=self.course, advisor=self.advisor, authors=[self.student], keywords=[]))
        with self._atomic_event():
            event = record_thesis_published(thesis)
        envelope = replace(EventEnvelope.from_outbox(event), event_version="2.0")
        self.assertEqual(handle_event(envelope), "REJECTED")
        self.assertFalse(PublicThesis.objects.filter(pk=thesis.pk).exists())

    def test_search_is_served_from_read_model(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Busca no read model", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Cache"], defense_date=date(2024, 6, 10)))
        project_thesis(str(thesis.pk))
        cache.clear()
        response = APIClient().get("/api/v1/public/theses", {"q": "read model"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.data["results"]], [str(thesis.pk)])
        detail = APIClient().get(f"/api/v1/public/theses/{thesis.pk}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["thesis"]["title"], "Busca no read model")

    def test_public_courses_endpoint_uses_the_read_model(self) -> None:
        response = APIClient().get("/api/v1/public/courses")
        self.assertEqual(response.status_code, 200)
        self.assertIn("results", response.data)

    def test_unpublish_event_removes_projection(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Será despublicada", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Teste"]))
        project_thesis(str(thesis.pk))
        self.assertTrue(PublicThesis.objects.filter(pk=thesis.pk).exists())
        thesis.status = ThesisStatus.ARCHIVED
        thesis.is_public = False
        thesis.save(update_fields=["status", "is_public"])
        with self._atomic_event():
            event = record_thesis_repository_updated(thesis, reason="UNPUBLISHED")
        handle_event(EventEnvelope.from_outbox(event))
        self.assertFalse(PublicThesis.objects.filter(pk=thesis.pk).exists())

    @staticmethod
    def _atomic_event():
        from contextlib import contextmanager
        from django.db import transaction

        @contextmanager
        def manager():
            with transaction.atomic():
                yield
        return manager()
