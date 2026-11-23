"""Curated test suites.

``FAST_TEST_LABELS`` is meant to run on every change (target: under two
minutes). It concentrates on domain rules, permissions, cross-service routing,
state transitions and API contracts that are most likely to regress when
services/databases change. The complete suite (``manage.py test``) remains the
slow, detailed gate for large changes.
"""

from __future__ import annotations

FAST_TEST_LABELS: tuple[str, ...] = (
    "thesismanagerbackend.tests.test_database_router",
    "thesismanagerbackend.tests.test_service_apis",
    "thesismanagerbackend.tests.test_ensure_service_databases",
    "thesismanagerbackend.tests.test_upload_throttle",
    "thesismanagerbackend.tests.test_test_isolation",
    "thesismanagerbackend.tests.test_compose_media",
    "thesismanagerbackend.tests.test_cross_database_queries",
    "thesismanagerbackend.tests.test_transaction_aliases",
    "thesismanagerbackend.tests.test_consumer_alias_order",
    "thesismanagerbackend.tests.test_migration_invariants",
    "thesismanagerbackend.tests.test_invitation_cross_database",
    "theses.tests.test_cross_service_assignments",
    "theses.tests.test_defense_snapshot",
    "theses.tests.test_published_edit",
    "theses.tests.test_repository_protection",
    "theses.tests.test_auto_publication",
    "theses.tests.test_public_repository_cache",
    "theses.tests.test_author_groups",
    "theses.tests.test_thesis_course_scope",
    "theses.tests.test_defense_coordinator_scope",
    "theses.tests.test_defense_external_access",
    "theses.tests.test_defense_status",
    "accounts.tests.test_coordinator_course_assignment",
    "accounts.tests.test_service_introspection",
    "events.tests.test_outbox",
    "events.tests.test_envelope",
    "events.tests.test_cleanup_events",
    "notifications.tests.test_workflow_events",
    "notifications.tests.test_member_invited",
    "reports.tests.test_overview_csv",
    "repository_readmodel.tests.test_projections",
    "reports_readmodel.tests.test_overview",
    "academic.tests.test_course_payload",
    "adjudication.tests.test_consumer",
    "indexing.tests.test_consumer",
)
