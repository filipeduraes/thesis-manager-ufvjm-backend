"""
Tests of the reporting read model and its service API.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import ProfileRole, User, UserProfileRole
from events.catalog import EventType
from events.envelope import EventEnvelope
from reports_readmodel.projections import handle_event
from reports_readmodel.models import ReportFact
from reports_readmodel.services import get_overview_payload
from theses.models import ThesisStatus


@override_settings(ROOT_URLCONF="thesismanagerbackend.service_urls.reports")
class ReportsReadModelTests(TestCase):
    """Aggregates come only from ReportFact rows."""

    def setUp(self) -> None:
        self.coordinator = User.objects.create_user(email="coord@ufvjm.edu.br", password="senha123", name="Coord")
        UserProfileRole.objects.create(user=self.coordinator, role=ProfileRole.COORDINATOR)
        self.client = APIClient()
        self.client.force_authenticate(user=self.coordinator)

    def test_unsupported_event_version_is_rejected(self) -> None:
        envelope = EventEnvelope(
            event_id=uuid4(),
            event_type=EventType.THESIS_SUBMITTED,
            event_version="2.0",
            occurred_at=timezone.now(),
            aggregate_type="Thesis",
            aggregate_id=uuid4(),
            correlation_id=None,
            causation_id=None,
            payload={"thesisId": str(uuid4()), "submittedAt": timezone.now().isoformat()},
        )
        self.assertEqual(handle_event(envelope), "REJECTED")
        self.assertEqual(ReportFact.objects.count(), 0)

    def test_aggregates_are_computed_from_facts(self) -> None:
        now = timezone.now()
        course_id = uuid4()
        ReportFact.objects.create(
            thesis_id=uuid4(), course_id=course_id, course_name="Computação", course_code="CC",
            submitted_at=now - timedelta(days=30), defense_at=now, defense_year=now.year,
            result="APPROVED", thesis_status=ThesisStatus.APPROVED,
        )
        ReportFact.objects.create(
            thesis_id=uuid4(), course_id=course_id, course_name="Computação", course_code="CC",
            result="REJECTED", thesis_status=ThesisStatus.REJECTED,
        )
        payload = get_overview_payload({})
        self.assertEqual(payload["summary"]["defensesCompletedCount"], 2)
        self.assertEqual(payload["summary"]["approvalRate"], 50.0)
        self.assertEqual(payload["summary"]["averageDaysToDefense"], 30.0)
        self.assertEqual(payload["byCourse"][0]["thesisCount"], 2)

    def test_service_endpoint_uses_read_model(self) -> None:
        ReportFact.objects.create(thesis_id=uuid4(), defense_year=2025, result="APPROVED_WITH_REMARKS")
        response = self.client.get("/api/v1/reports/overview", {"year": 2025})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["summary"]["approvedWithRemarksCount"], 1)
