"""
Tests of the independent HTTP API of each service.
"""

from __future__ import annotations

from django.test import Client, TestCase, override_settings

SERVICE_MODULES = {
    "auth": "thesismanagerbackend.service_urls.auth",
    "theses": "thesismanagerbackend.service_urls.theses",
    "committee": "thesismanagerbackend.service_urls.committee",
    "repository": "thesismanagerbackend.service_urls.repository",
    "notifications": "thesismanagerbackend.service_urls.notifications",
    "reports": "thesismanagerbackend.service_urls.reports",
}


class ServiceApiTests(TestCase):
    """Each service exposes its own routes and not the others'."""

    def test_every_service_has_a_health_route(self) -> None:
        for name, module in SERVICE_MODULES.items():
            with self.subTest(service=name), override_settings(ROOT_URLCONF=module):
                response = Client().get("/health")
                self.assertEqual(response.status_code, 200)

    @override_settings(ROOT_URLCONF=SERVICE_MODULES["repository"])
    def test_repository_service_does_not_expose_theses_or_reports(self) -> None:
        self.assertNotEqual(Client().get("/api/v1/public/theses").status_code, 404)
        self.assertEqual(Client().get("/api/v1/theses").status_code, 404)
        self.assertEqual(Client().get("/api/v1/reports/overview").status_code, 404)

    @override_settings(ROOT_URLCONF=SERVICE_MODULES["committee"])
    def test_committee_service_does_not_expose_public_repository(self) -> None:
        self.assertNotEqual(Client().get("/api/v1/defenses").status_code, 404)
        self.assertEqual(Client().get("/api/v1/public/theses").status_code, 404)
        self.assertEqual(Client().get("/api/v1/theses").status_code, 404)

    @override_settings(ROOT_URLCONF=SERVICE_MODULES["reports"])
    def test_reports_service_only_exposes_reports(self) -> None:
        self.assertNotEqual(Client().get("/api/v1/reports/overview").status_code, 404)
        self.assertEqual(Client().get("/api/v1/public/theses").status_code, 404)

    @override_settings(ROOT_URLCONF=SERVICE_MODULES["auth"])
    def test_auth_service_exposes_session_and_advisors(self) -> None:
        self.assertEqual(Client().get("/api/v1/session").status_code, 200)
        self.assertEqual(Client().get("/api/v1/public/advisors").status_code, 200)
        self.assertEqual(Client().get("/api/v1/reports/overview").status_code, 404)
