"""
Characterization tests for the audit presentation layer.

These tests ensure that structural refactors (labels derived from TextChoices,
category map and export columns) preserve the observable API contract.
"""

from __future__ import annotations

from rest_framework.test import APIClient

from accounts.audit_identifiers import EntityType
from accounts.audit_data import resolve_audit_events
from accounts.audit_entry import AuditEntry
from accounts.audit_identifiers import AuditCategory, AuditEvent, EntityType
from accounts.audit_presentation import (
    CATEGORY_LABELS,
    COMMITTEE_ROLE_LABELS,
    DEFENSE_RESULT_LABELS,
    DEFENSE_STATUS_LABELS,
    DOCUMENT_TYPE_LABELS,
    INVITATION_STATUS_LABELS,
    REVIEW_DECISION_LABELS,
    THESIS_STATUS_LABELS,
    AuditPresenter,
    choices_labels,
)
from accounts.models import AuditAction, InvitationStatus, ProfileRole, User, UserProfileRole
from accounts.serializers import AuditLogSerializer
from accounts.services import record_audit
from django.test import TestCase, override_settings
from theses.models import AvailabilityStatus, CommitteeRole, DefenseResult, DefenseStatus, DocumentType, ReviewDecision, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class AuditLabelTests(TestCase):
    """
    Verify that displayed labels come from the model enums.
    """

    def test_label_maps_match_model_choices(self) -> None:
        self.assertEqual(INVITATION_STATUS_LABELS, choices_labels(InvitationStatus))
        self.assertEqual(DEFENSE_STATUS_LABELS, choices_labels(DefenseStatus))
        self.assertEqual(THESIS_STATUS_LABELS, choices_labels(ThesisStatus))
        self.assertEqual(DEFENSE_RESULT_LABELS, choices_labels(DefenseResult))
        self.assertEqual(REVIEW_DECISION_LABELS, choices_labels(ReviewDecision))
        self.assertEqual(COMMITTEE_ROLE_LABELS, choices_labels(CommitteeRole))
        self.assertEqual(DOCUMENT_TYPE_LABELS, choices_labels(DocumentType))

    def test_category_labels_are_stable(self) -> None:
        self.assertEqual(
            CATEGORY_LABELS,
            {
                "ACCESS": "Acesso",
                "MANAGEMENT": "Gestão",
                "THESIS": "Monografia e defesa",
                "PUBLICATION": "Publicação",
                "ERROR": "Erro",
            },
        )

    def test_details_keep_readable_labels(self) -> None:
        log = record_audit(AuditEntry(
            action=AuditAction.CREATE,
            entity_type=EntityType.COURSE,
            entity_id="11111111-1111-1111-1111-111111111111",
            metadata={"name": "Engenharia", "code": "ENG"},
        ))
        presenter = AuditPresenter()
        presenter.prime([log])
        labels = [row["label"] for row in presenter.details(log)]
        self.assertIn("Curso", labels)
        self.assertIn("Código", labels)


@override_settings(CACHES=TEST_CACHES, SESSION_ENGINE="django.contrib.sessions.backends.db")
class AuditExportContractTests(TestCase):
    """
    Verify that CSV and JSON export the same serializer fields.
    """

    def setUp(self) -> None:
        user = User.objects.create_user(email="admin@ufvjm.edu.br", name="Admin", password="x")
        UserProfileRole.objects.create(user=user, role=ProfileRole.ADMIN)
        self.client = APIClient()
        self.client.force_authenticate(user=user)

    def test_csv_header_matches_serializer_fields(self) -> None:
        response = self.client.get("/api/v1/admin/audit-logs?export=csv")
        self.assertEqual(response.status_code, 200)
        header = response.content.decode("utf-8-sig").splitlines()[0].split(",")
        self.assertEqual(header, list(AuditLogSerializer.Meta.fields))

    def test_typed_identifiers_are_persisted_as_original_strings(self) -> None:
        from accounts.audit_identifiers import AuditEvent, EntityType

        entry = record_audit(
            AuditEntry(
                action=AuditAction.UPDATE,
                entity_type=EntityType.DEFENSE,
                entity_id="22222222-2222-2222-2222-222222222222",
                metadata={"event": AuditEvent.DEFENSE_CLOSED},
            )
        )
        entry.refresh_from_db()
        self.assertEqual(entry.entity_type, "Defense")
        self.assertEqual(entry.entity_app_label, "theses")
        self.assertEqual(entry.metadata["event"], "DEFENSE_CLOSED")

    def test_entity_type_has_label_and_app_label(self) -> None:
        from accounts.audit_identifiers import EntityType

        self.assertEqual(EntityType.USER.label, "Usuário")
        self.assertEqual(EntityType.USER.app_label, "accounts")
        self.assertEqual(EntityType.REPORT_OVERVIEW.app_label, "reports")

    def test_field_parse_rejects_unknown_key(self) -> None:
        from accounts.audit_identifiers import AuditField

        self.assertEqual(AuditField.parse("versionNumber"), AuditField.VERSION_NUMBER)
        self.assertIsNone(AuditField.parse("unknownField"))

    def test_event_enum_serializes_as_stored_string(self) -> None:
        import json

        from accounts.audit_identifiers import AuditEvent

        self.assertEqual(
            json.dumps({"event": AuditEvent.SUBMITTED}),
            '{"event": "SUBMITTED"}',
        )

    def test_api_returns_structured_event(self) -> None:
        from accounts.audit_identifiers import AuditCategory, AuditEvent, EntityType

        record_audit(
            AuditEntry(
                action=AuditAction.CREATE,
                entity_type=EntityType.COURSE,
                entity_id="33333333-3333-3333-3333-333333333333",
                metadata={"name": "Engenharia", "code": "ENG"},
            )
        )
        response = self.client.get("/api/v1/admin/audit-logs?page_size=5")
        self.assertEqual(response.status_code, 200)
        event = response.json()["results"][0]["event"]
        self.assertEqual(event["action"], "CREATE")
        self.assertEqual(event["entity"]["type"], "Course")
        self.assertEqual(event["entity"]["name"], "Engenharia")
        self.assertEqual(event["category"], AuditCategory.MANAGEMENT.value)

    def test_csv_export_uses_structured_layer(self) -> None:
        from accounts.audit_export import build_export_row
        from accounts.audit_data import resolve_audit_events

        log = record_audit(
            AuditEntry(
                action=AuditAction.LOGIN,
                entity_type=EntityType.USER,
                entity_id="44444444-4444-4444-4444-444444444444",
            )
        )
        event = resolve_audit_events([log])[0]
        row = build_export_row(event, event.actor.name, event.actor.roles)
        self.assertEqual(row["actionCode"], "LOGIN")
        self.assertEqual(row["entityType"], "User")
        self.assertEqual(row["category"], event.category.value)


class TypedEventPayloadTests(TestCase):
    """Event payloads have explicit contracts and deterministic serialization."""

    def test_evaluation_payload_has_typed_fields(self) -> None:
        from accounts.audit_events import EvaluationPayload, event_payload

        payload = event_payload(
            AuditEvent.EVALUATION_SUBMITTED,
            memberId="m1",
            decision="APPROVED",
            score="9.50",
        )
        self.assertEqual(payload["memberId"], "m1")
        self.assertEqual(payload["score"], "9.50")
        self.assertEqual(set(payload), {"event", "schemaVersion", "memberId", "decision", "score"})

    def test_unknown_field_is_rejected(self) -> None:
        from accounts.audit_events import event_payload

        with self.assertRaises(ValueError):
            event_payload(AuditEvent.EVALUATION_SUBMITTED, typoField="x")

    def test_payload_is_persisted_with_schema_version(self) -> None:
        from accounts.audit_events import event_payload

        metadata = event_payload(AuditEvent.EVALUATION_SUBMITTED, memberId="m1", decision="APPROVED")
        entry = record_audit(
            AuditEntry(
                action=AuditAction.EVALUATE,
                entity_type=EntityType.DEFENSE,
                entity_id="55555555-5555-5555-5555-555555555555",
                metadata=metadata,
            )
        )
        entry.refresh_from_db()
        self.assertEqual(entry.metadata["schemaVersion"], 2)
        self.assertEqual(entry.metadata["event"], "EVALUATION_SUBMITTED")
