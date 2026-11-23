"""
Tests for the anonymous public repository: search, filters, payloads and limits.
"""

from __future__ import annotations

from datetime import date, datetime, timezone as dt_timezone
from unittest.mock import patch

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.throttling import ScopedRateThrottle

from accounts.models import ProfileRole
from theses.models import Defense, DefenseStatus, DocumentType, Thesis, ThesisStatus
from theses.services import publish_thesis, resolve_defense_date, upload_thesis_document, validate_thesis_document
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user, pdf_upload


class PublicSearchTests(RepositoryTestCase):
    """Search by every field, combined filters, visibility and ordering."""

    def setUp(self) -> None:
        super().setUp()
        self.distributed = create_thesis(ThesisSpec(
            title="Sistemas distribuídos com filas",
            course=self.course,
            advisor=self.advisor,
            authors=[self.student],
            keywords=["RabbitMQ", "Mensageria"],
            abstract="Estudo sobre filas de eventos.",
            defense_date=date(2019, 11, 5),
        ))
        self.vision = create_thesis(ThesisSpec(
            title="Visão computacional aplicada",
            course=self.other_course,
            advisor=self.other_advisor,
            authors=[self.other_student],
            keywords=["Redes neurais"],
            abstract="Uso de sistemas distribuídos para treinar modelos.",
            defense_date=date(2031, 3, 2),
        ))
        self.cache_study = create_thesis(ThesisSpec(
            title="Cache Redis em repositórios",
            course=self.course,
            advisor=self.other_advisor,
            authors=[self.student, self.other_student],
            keywords=["Redis", "mensageria"],
            defense_date=date(2026, 7, 1),
        ))

    def search(self, **params: object) -> list[str]:
        response = self.client.get("/api/v1/public/theses", params)
        self.assertEqual(response.status_code, 200, response.content)
        return [item["id"] for item in response.data["results"]]

    def test_anonymous_can_search_without_login(self) -> None:
        response = self.client.get("/api/v1/public/theses")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 3)
        for key in ("count", "page", "pageSize", "totalPages", "lastPageCount", "results"):
            self.assertIn(key, response.data)

    def test_search_by_each_field(self) -> None:
        self.assertEqual(self.search(title="visão"), [str(self.vision.pk)])
        self.assertEqual(self.search(author="diego"), [str(self.vision.pk), str(self.cache_study.pk)])
        self.assertEqual(self.search(advisor="ana orient"), [str(self.distributed.pk)])
        self.assertEqual(self.search(advisorId=str(self.other_advisor.pk)), [str(self.vision.pk), str(self.cache_study.pk)])
        self.assertEqual(self.search(course="ciência"), [str(self.vision.pk)])
        self.assertEqual(self.search(course="si"), [str(self.cache_study.pk), str(self.distributed.pk)])
        self.assertEqual(self.search(courseId=str(self.other_course.pk)), [str(self.vision.pk)])
        self.assertEqual(self.search(year=2019), [str(self.distributed.pk)])
        self.assertEqual(self.search(keyword="REDIS"), [str(self.cache_study.pk)])

    def test_free_text_matches_title_author_advisor_course_and_keyword(self) -> None:
        self.assertEqual(self.search(q="Carla"), [str(self.cache_study.pk), str(self.distributed.pk)])
        self.assertEqual(self.search(q="bruno"), [str(self.vision.pk), str(self.cache_study.pk)])
        self.assertEqual(self.search(q="rabbitmq"), [str(self.distributed.pk)])
        self.assertEqual(self.search(q="Ciência da Computação"), [str(self.vision.pk)])

    def test_combined_filters_are_intersected(self) -> None:
        self.assertEqual(self.search(keyword="mensageria", courseId=str(self.course.pk), year=2026), [str(self.cache_study.pk)])
        self.assertEqual(self.search(keyword=["mensageria", "rabbitmq"]), [str(self.distributed.pk)])
        self.assertEqual(self.search(q="treinar", advisor="bruno"), [str(self.vision.pk)])
        self.assertEqual(self.search(author="carla", year=2031), [])

    def test_relevance_ranks_title_matches_above_abstract_matches(self) -> None:
        response = self.client.get("/api/v1/public/theses", {"q": "sistemas distribuídos"})
        self.assertEqual(response.data["ordering"], "relevance")
        self.assertEqual([item["id"] for item in response.data["results"]], [str(self.distributed.pk), str(self.vision.pk)])

    def test_recent_and_title_ordering(self) -> None:
        self.assertEqual(self.search(), [str(self.vision.pk), str(self.cache_study.pk), str(self.distributed.pk)])
        self.assertEqual(self.search(ordering="recent", q="sistemas"), [str(self.vision.pk), str(self.cache_study.pk), str(self.distributed.pk)])
        self.assertEqual(self.search(ordering="title"), [str(self.cache_study.pk), str(self.distributed.pk), str(self.vision.pk)])

    def test_pagination(self) -> None:
        response = self.client.get("/api/v1/public/theses", {"pageSize": 2, "page": 2})
        self.assertEqual(response.data["count"], 3)
        self.assertEqual(response.data["totalPages"], 2)
        self.assertEqual(response.data["lastPageCount"], 1)
        self.assertEqual([item["id"] for item in response.data["results"]], [str(self.distributed.pk)])

    def test_invalid_parameters_return_400(self) -> None:
        for params in ({"year": "dois mil"}, {"courseId": "abc"}, {"advisorId": "1"}, {"ordering": "random"}, {"year": "1200"}):
            response = self.client.get("/api/v1/public/theses", params)
            self.assertEqual(response.status_code, 400, params)

    def test_unpublished_and_deleted_theses_never_appear(self) -> None:
        hidden_titles = {
            ThesisStatus.DRAFT: "Rascunho sigiloso",
            ThesisStatus.UNDER_EVALUATION: "Em avaliação sigilosa",
            ThesisStatus.APPROVED: "Aprovada não publicada",
        }
        hidden = [
            create_thesis(ThesisSpec(title=title, course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Oculta"], status=status))
            for status, title in hidden_titles.items()
        ]
        deleted = create_thesis(ThesisSpec(title="Publicada e excluída", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Oculta"]))
        deleted.deleted_at = timezone.now()
        deleted.save(update_fields=["deleted_at"])
        hidden.append(deleted)
        self.assertEqual(self.search(q="sigilos"), [])
        self.assertEqual(self.search(keyword="oculta"), [])
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 3)
        filters = self.client.get("/api/v1/public/theses/filters").data
        self.assertNotIn("oculta", [item["value"] for item in filters["keywords"]])
        for thesis in hidden:
            self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}").status_code, 404)
            self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}/download").status_code, 404)

    def test_list_query_count_does_not_grow_with_results(self) -> None:
        with CaptureQueriesContext(connection) as three_results:
            self.client.get("/api/v1/public/theses")
        for index in range(4):
            create_thesis(ThesisSpec(title=f"Extra {index}", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Extra"]))
        cache.clear()
        with CaptureQueriesContext(connection) as seven_results:
            response = self.client.get("/api/v1/public/theses")
        self.assertEqual(response.data["count"], 7)
        self.assertEqual(len(seven_results), len(three_results))


class PublicPayloadTests(RepositoryTestCase):
    """Anonymous payloads expose public URLs and the SHA-256, never e-mails."""

    def setUp(self) -> None:
        super().setUp()
        self.thesis = create_thesis(ThesisSpec(title="Integridade de PDFs", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Hash"]))

    def test_list_payload_uses_public_urls_and_hides_emails(self) -> None:
        response = self.client.get("/api/v1/public/theses")
        item = response.data["results"][0]
        self.assertEqual(item["currentFile"]["downloadUrl"], f"/api/v1/public/theses/{self.thesis.pk}/download")
        self.assertEqual(item["student"], {"id": str(self.student.pk), "name": self.student.name})
        self.assertEqual(item["advisor"], {"id": str(self.advisor.pk), "name": self.advisor.name})
        self.assertEqual(item["authorNames"], [self.student.name])
        self.assertEqual(item["defenseYear"], 2024)
        self.assertNotIn("@", response.content.decode())

    def test_detail_exposes_integrity_hash(self) -> None:
        response = self.client.get(f"/api/v1/public/theses/{self.thesis.pk}")
        self.assertEqual(response.status_code, 200)
        payload = response.data["thesis"]
        current = self.thesis.files.get(is_current=True)
        self.assertEqual(payload["integrity"]["algorithm"], "SHA-256")
        self.assertEqual(payload["integrity"]["sha256"], current.stored_file.sha256_hex)
        self.assertEqual(payload["currentFile"]["storedFile"]["sha256"], current.stored_file.sha256_hex)
        self.assertEqual(payload["fileVersions"][0]["sha256"], current.stored_file.sha256_hex)
        self.assertEqual(payload["currentFile"]["previewUrl"], f"/api/v1/public/theses/{self.thesis.pk}/download?disposition=inline")
        self.assertNotIn("@", response.content.decode())

    def test_download_streams_pdf_and_errors_are_json(self) -> None:
        response = self.client.get(f"/api/v1/public/theses/{self.thesis.pk}/download")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        missing = self.client.get("/api/v1/public/theses/00000000-0000-0000-0000-000000000000/download", HTTP_ACCEPT="application/pdf")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing["Content-Type"], "application/json")

    def test_tampered_file_is_not_served(self) -> None:
        current = self.thesis.files.get(is_current=True)
        with open(f"{self.media_root}/{current.stored_file.storage_path}", "ab") as handle:
            handle.write(b"alterado")
        with self.assertLogs("theses.views", level="ERROR"):
            response = self.client.get(f"/api/v1/public/theses/{self.thesis.pk}/download")
        self.assertEqual(response.status_code, 404)


class PublicFiltersAndCoursesTests(RepositoryTestCase):
    """Filter options come from published data, including years beyond 2025."""

    def setUp(self) -> None:
        super().setUp()
        create_thesis(ThesisSpec(title="Primeira", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Redes", "IoT"], defense_date=date(2012, 5, 1)))
        create_thesis(ThesisSpec(title="Segunda", course=self.course, advisor=self.advisor, authors=[self.other_student], keywords=["redes"], defense_date=date(2033, 8, 9)))
        create_thesis(ThesisSpec(title="Não publicada", course=self.other_course, advisor=self.other_advisor, authors=[self.student], keywords=["Privada"], status=ThesisStatus.APPROVED))

    def test_years_advisors_courses_and_keywords_are_dynamic(self) -> None:
        response = self.client.get("/api/v1/public/theses/filters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["years"], [{"year": 2033, "count": 1}, {"year": 2012, "count": 1}])
        self.assertEqual(response.data["advisors"], [{"id": str(self.advisor.pk), "name": self.advisor.name, "count": 2}])
        self.assertEqual(response.data["courses"], [{"id": str(self.course.pk), "name": self.course.name, "code": "SI", "count": 2}])
        self.assertEqual(response.data["keywords"][0], {"name": "Redes", "value": "redes", "count": 2})
        self.assertEqual({item["value"] for item in response.data["keywords"]}, {"redes", "iot"})

    def test_year_filter_accepts_any_year_present_in_data(self) -> None:
        response = self.client.get("/api/v1/public/theses", {"year": 2033})
        self.assertEqual([item["title"] for item in response.data["results"]], ["Segunda"])

    def test_public_courses_list_counts_published_theses(self) -> None:
        response = self.client.get("/api/v1/public/courses")
        self.assertEqual(response.status_code, 200)
        counts = {item["code"]: item["publishedThesesCount"] for item in response.data["results"]}
        self.assertEqual(counts, {"SI": 2, "CC": 0})
        only_published = self.client.get("/api/v1/public/courses", {"withPublished": "true"})
        self.assertEqual([item["code"] for item in only_published.data["results"]], ["SI"])
        self.assertNotIn("coordinator", only_published.data["results"][0])


class PublicThrottleTests(RepositoryTestCase):
    """Public endpoints are rate limited per client IP, ignoring forged forwarding headers."""

    def _rates(self, **overrides: str) -> dict[str, str]:
        return {**ScopedRateThrottle.THROTTLE_RATES, **overrides}

    def test_repository_requests_are_throttled(self) -> None:
        with patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates(public_repository="2/min")):
            self.assertEqual(self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="10.0.0.1").status_code, 200)
            self.assertEqual(self.client.get("/api/v1/public/courses", HTTP_X_FORWARDED_FOR="10.0.0.2").status_code, 200)
            blocked = self.client.get("/api/v1/public/theses/filters", HTTP_X_FORWARDED_FOR="10.0.0.3")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked["Content-Type"], "application/json")

    def test_forwarded_header_from_untrusted_peer_does_not_open_new_buckets(self) -> None:
        with self.settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_CIDRS=["10.9.0.0/16"]):
            with patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates(public_repository="1/min")):
                self.assertEqual(self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="203.0.113.1").status_code, 200)
                blocked = self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="203.0.113.2")
        self.assertEqual(blocked.status_code, 429)

    def test_forwarded_header_from_trusted_proxy_identifies_the_client(self) -> None:
        with self.settings(TRUSTED_PROXY_COUNT=1, TRUSTED_PROXY_CIDRS=["127.0.0.0/8"]):
            with patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates(public_repository="1/min")):
                self.assertEqual(self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="203.0.113.1").status_code, 200)
                other_client = self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="203.0.113.2")
                same_client = self.client.get("/api/v1/public/theses", HTTP_X_FORWARDED_FOR="203.0.113.1")
        self.assertEqual(other_client.status_code, 200)
        self.assertEqual(same_client.status_code, 429)

    def test_download_has_its_own_scope(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Download limitado", course=self.course, advisor=self.advisor, authors=[self.student]))
        with patch.object(ScopedRateThrottle, "THROTTLE_RATES", self._rates(public_download="1/min")):
            self.assertEqual(self.client.get(f"/api/v1/public/theses/{thesis.pk}/download").status_code, 200)
            blocked = self.client.get(f"/api/v1/public/theses/{thesis.pk}/download", HTTP_ACCEPT="application/pdf")
            self.assertEqual(self.client.get("/api/v1/public/theses").status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked["Content-Type"], "application/json")


class DefenseDateOnPublicationTests(RepositoryTestCase):
    """Publication records the defense date used by the year filter."""

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenação", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])

    def _publish(self, thesis: Thesis) -> Thesis:
        authorization = upload_thesis_document(self.student, thesis, DocumentType.PUBLICATION_AUTHORIZATION, pdf_upload("autorizacao.pdf"))
        validate_thesis_document(self.coordinator, thesis, authorization)
        thesis.refresh_from_db()
        if thesis.status != ThesisStatus.PUBLISHED:
            thesis = publish_thesis(self.coordinator, thesis)
        thesis.refresh_from_db()
        return thesis

    def test_completed_defense_date_is_stored_in_sao_paulo_time(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Aprovada em banca", course=self.course, advisor=self.advisor, authors=[self.student], status=ThesisStatus.APPROVED))
        Defense.objects.create(thesis=thesis, scheduled_datetime=datetime(2030, 1, 1, 1, 30, tzinfo=dt_timezone.utc), status=DefenseStatus.COMPLETED)
        thesis = self._publish(thesis)
        self.assertEqual(thesis.defense_date, date(2029, 12, 31))
        self.assertEqual(thesis.defense_year, 2029)
        response = self.client.get("/api/v1/public/theses", {"year": 2029})
        self.assertEqual([item["id"] for item in response.data["results"]], [str(thesis.pk)])

    def test_manual_result_date_is_the_fallback(self) -> None:
        thesis = create_thesis(ThesisSpec(title="Resultado manual", course=self.course, advisor=self.advisor, authors=[self.student], status=ThesisStatus.APPROVED))
        entry = thesis.status_history.create(previous_status=ThesisStatus.ADVISOR_APPROVED, new_status=ThesisStatus.APPROVED, metadata={"defenseResult": "APPROVED"})
        entry.changed_at = datetime(2021, 4, 2, 15, 0, tzinfo=dt_timezone.utc)
        entry.save(update_fields=["changed_at"])
        self.assertEqual(resolve_defense_date(thesis), date(2021, 4, 2))
        thesis = self._publish(thesis)
        self.assertEqual(thesis.defense_year, 2021)


class PostgresFullTextSearchTests(RepositoryTestCase):
    """PostgreSQL builds a tsvector query compatible with the GIN index."""

    def test_postgres_search_uses_to_tsvector(self) -> None:
        from theses.repository import PublicSearchFilters, search_public_theses

        filters = PublicSearchFilters(query="sistemas distribuidos")
        with patch.object(connection, "vendor", "postgresql"):
            queryset = search_public_theses(filters)
            sql = str(queryset.query)
        self.assertIn("to_tsvector", sql)
        self.assertIn("portuguese", sql)
        self.assertIn("websearch_to_tsquery", sql)

    def test_sqlite_keeps_the_portable_filter(self) -> None:
        from theses.repository import PublicSearchFilters, search_public_theses

        filters = PublicSearchFilters(query="sistemas distribuidos")
        with patch.object(connection, "vendor", "sqlite"):
            queryset = search_public_theses(filters)
            sql = str(queryset.query)
        self.assertNotIn("to_tsvector", sql)
        self.assertIn("search_text", sql)
