"""
Tests for the Redis cache of the public repository and its invalidation.
"""

from __future__ import annotations

import re

from django.core.cache import cache

from accounts.models import ProfileRole
from theses.models import DocumentType, ThesisStatus
from theses.repository import PublicSearchFilters
from theses.repository_cache import VERSION_KEY, invalidate_public_repository_cache, search_cache_key
from theses.services import publish_thesis, upload_thesis_document, validate_thesis_document
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user, pdf_upload

SEARCH_KEY_PATTERN = re.compile(r"^busca:[0-9a-f]{64}$")


class SearchCacheTests(RepositoryTestCase):
    """Repeated searches are served from the cache under ``busca:<hash>``."""

    def setUp(self) -> None:
        super().setUp()
        self.thesis = create_thesis(ThesisSpec(title="Cache de busca", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Redis"]))

    def test_second_identical_search_is_a_cache_hit_without_queries(self) -> None:
        first = self.client.get("/api/v1/public/theses", {"q": "cache"})
        self.assertEqual(first["X-Repository-Cache"], "MISS")
        with self.assertNumQueries(0):
            second = self.client.get("/api/v1/public/theses", {"q": "cache"})
        self.assertEqual(second["X-Repository-Cache"], "HIT")
        self.assertEqual(second.json(), first.json())

    def test_key_format_and_normalization(self) -> None:
        filters = PublicSearchFilters(query="Cache")
        key = search_cache_key(filters, "1", "")
        self.assertRegex(key, SEARCH_KEY_PATTERN)
        self.assertEqual(key, search_cache_key(PublicSearchFilters(query="cache"), "1", ""))
        self.assertNotEqual(key, search_cache_key(filters, "2", ""))
        self.assertNotEqual(key, search_cache_key(PublicSearchFilters(query="cache", year=2024), "1", ""))
        self.client.get("/api/v1/public/theses", {"q": "Cache"})
        self.assertIsNotNone(cache.get(key))

    def test_query_text_is_not_stored_in_the_key(self) -> None:
        key = search_cache_key(PublicSearchFilters(query="termo pessoal"), "1", "")
        self.assertNotIn("termo", key)

    def test_detail_filters_and_courses_are_cached(self) -> None:
        for path in (f"/api/v1/public/theses/{self.thesis.pk}", "/api/v1/public/theses/filters", "/api/v1/public/courses"):
            self.assertEqual(self.client.get(path)["X-Repository-Cache"], "MISS", path)
            with self.assertNumQueries(0):
                self.assertEqual(self.client.get(path)["X-Repository-Cache"], "HIT", path)

    def test_invalid_parameters_are_not_cached(self) -> None:
        self.assertEqual(self.client.get("/api/v1/public/theses", {"year": "x"}).status_code, 400)
        self.assertEqual(self.client.get("/api/v1/public/theses/00000000-0000-0000-0000-000000000000").status_code, 404)

    def test_invalidation_rotates_the_version(self) -> None:
        self.client.get("/api/v1/public/theses")
        version = cache.get(VERSION_KEY)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            invalidate_public_repository_cache()
        self.assertEqual(len(callbacks), 1)
        self.assertNotEqual(cache.get(VERSION_KEY), version)
        self.assertEqual(self.client.get("/api/v1/public/theses")["X-Repository-Cache"], "MISS")


class PublicationInvalidatesCacheTests(RepositoryTestCase):
    """Publishing a thesis makes it visible immediately despite cached pages."""

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenação", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])

    def test_publication_invalidates_search_filters_and_courses(self) -> None:
        create_thesis(ThesisSpec(title="Já publicada", course=self.course, advisor=self.advisor, authors=[self.student]))
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 1)
        self.assertEqual(len(self.client.get("/api/v1/public/theses/filters").data["years"]), 1)
        thesis = create_thesis(ThesisSpec(title="Nova publicação", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.APPROVED))
        self.assertEqual(self.client.get("/api/v1/public/theses")["X-Repository-Cache"], "HIT")
        with self.captureOnCommitCallbacks(execute=True):
            authorization = upload_thesis_document(self.other_student, thesis, DocumentType.PUBLICATION_AUTHORIZATION, pdf_upload("autorizacao.pdf"))
            validate_thesis_document(self.coordinator, thesis, authorization)
            thesis.refresh_from_db()
            if thesis.status != ThesisStatus.PUBLISHED:
                publish_thesis(self.coordinator, thesis)
        response = self.client.get("/api/v1/public/theses")
        self.assertEqual(response["X-Repository-Cache"], "MISS")
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}").status_code, 200)
        courses = self.client.get("/api/v1/public/courses").data["results"]
        self.assertEqual({item["code"]: item["publishedThesesCount"] for item in courses}["SI"], 2)
