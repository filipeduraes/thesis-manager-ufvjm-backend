"""
Tests for automatic defense status sync and admin status revert.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import AuditLog, ProfileRole, User, UserProfileRole
from theses.defense_committee import add_institutional_member
from theses.defense_scheduling import schedule_defense, start_defense, sync_defense_status
from theses.models import CommitteeRole, Defense, DefenseStatus, Thesis, ThesisStatus
from theses.tests.defense_helpers import record_evaluation_without_auto_close

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def _make_user(email: str, name: str, role: str) -> User:
    u = User.objects.create_user(email=email, password="x", name=name)
    UserProfileRole.objects.create(user=u, role=role)
    return u


# ─── sync_defense_status (service level) ─────────────────────────────────────

@override_settings(CACHES=TEST_CACHES)
class SyncDefenseStatusServiceTests(TestCase):
    """Service-level tests for sync_defense_status."""

    def setUp(self):
        dept = Department.objects.create(name="CC-Sync-Svc")
        self.admin = _make_user("adm_sync_svc@t.br", "Admin", ProfileRole.ADMIN)
        coord = _make_user("crd_sync_svc@t.br", "Coord", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=dept, coordinator=coord, name="CC", code="SS1")
        self.advisor_a = _make_user("adva1@t.br", "AdvA", ProfileRole.ADVISOR)
        self.advisor_b = _make_user("advb1@t.br", "AdvB", ProfileRole.ADVISOR)
        self.thesis_past = Thesis.objects.create(course=course, advisor=self.advisor_a, title="Past", status=ThesisStatus.ADVISOR_APPROVED)
        self.thesis_future = Thesis.objects.create(course=course, advisor=self.advisor_b, title="Future", status=ThesisStatus.ADVISOR_APPROVED)
        past = timezone.now() - timedelta(hours=1)
        future = timezone.now() + timedelta(hours=2)
        self.defense_past = schedule_defense(self.thesis_past, scheduled_datetime=past, actor=self.admin)
        self.defense_future = schedule_defense(self.thesis_future, scheduled_datetime=future, actor=self.admin)

    def test_past_scheduled_becomes_in_progress(self):
        result = sync_defense_status(self.defense_past)
        result.refresh_from_db()
        self.assertEqual(result.status, DefenseStatus.IN_PROGRESS)

    def test_future_scheduled_stays_scheduled(self):
        result = sync_defense_status(self.defense_future)
        result.refresh_from_db()
        self.assertEqual(result.status, DefenseStatus.SCHEDULED)

    def test_idempotent_does_not_duplicate_audit_log(self):
        sync_defense_status(self.defense_past)
        self.defense_past.refresh_from_db()
        sync_defense_status(self.defense_past)
        entries = AuditLog.objects.filter(
            entity_type="Defense",
            entity_id=str(self.defense_past.pk),
            metadata__event="AUTO_STARTED_BY_SCHEDULE",
        )
        self.assertEqual(entries.count(), 1)

    def test_auto_start_leaves_thesis_under_evaluation(self):
        sync_defense_status(self.defense_past)
        self.thesis_past.refresh_from_db()
        self.assertEqual(self.thesis_past.status, ThesisStatus.UNDER_EVALUATION)

    def test_auto_start_thesis_state_matches_manual_start(self):
        # Parallel: auto-start one defense, manually start the other
        dept = Department.objects.create(name="CC-Cmp")
        admin = self.admin
        coord = _make_user("crd_cmp@t.br", "CoordCmp", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=dept, coordinator=coord, name="Cmp", code="CMP1")
        adv1 = _make_user("advcmp1@t.br", "Adv1", ProfileRole.ADVISOR)
        adv2 = _make_user("advcmp2@t.br", "Adv2", ProfileRole.ADVISOR)
        t_auto = Thesis.objects.create(course=course, advisor=adv1, title="Auto", status=ThesisStatus.ADVISOR_APPROVED)
        t_manual = Thesis.objects.create(course=course, advisor=adv2, title="Manual", status=ThesisStatus.ADVISOR_APPROVED)
        past = timezone.now() - timedelta(hours=1)
        d_auto = schedule_defense(t_auto, scheduled_datetime=past, actor=admin)
        d_manual = schedule_defense(t_manual, scheduled_datetime=past, actor=admin)

        sync_defense_status(d_auto)
        start_defense(d_manual, actor=admin)

        t_auto.refresh_from_db()
        t_manual.refresh_from_db()
        self.assertEqual(t_auto.status, t_manual.status)


# ─── sync via GET detail (API level) ─────────────────────────────────────────

@override_settings(CACHES=TEST_CACHES)
class SyncDefenseStatusApiTests(TestCase):
    """API-level tests: GET defense detail triggers sync."""

    def setUp(self):
        self.client = APIClient()
        dept = Department.objects.create(name="CC-Sync-Api")
        self.admin = _make_user("adm_sync_api@t.br", "Admin", ProfileRole.ADMIN)
        coord = _make_user("crd_sync_api@t.br", "Coord", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=dept, coordinator=coord, name="CC", code="SA1")
        adv1 = _make_user("adv_sa1@t.br", "Adv1", ProfileRole.ADVISOR)
        adv2 = _make_user("adv_sa2@t.br", "Adv2", ProfileRole.ADVISOR)
        t_past = Thesis.objects.create(course=course, advisor=adv1, title="Past-API", status=ThesisStatus.ADVISOR_APPROVED)
        t_future = Thesis.objects.create(course=course, advisor=adv2, title="Future-API", status=ThesisStatus.ADVISOR_APPROVED)
        self.defense_past = schedule_defense(t_past, scheduled_datetime=timezone.now() - timedelta(hours=1), actor=self.admin)
        self.defense_future = schedule_defense(t_future, scheduled_datetime=timezone.now() + timedelta(hours=2), actor=self.admin)

    def test_get_detail_with_past_datetime_returns_in_progress(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense_past.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)

    def test_get_detail_with_future_datetime_returns_scheduled(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/defenses/{self.defense_future.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.SCHEDULED)

    def test_put_evaluation_with_scheduled_defense_returns_400(self):
        # defense_future is SCHEDULED (future datetime); committee member tries to evaluate → 400
        evaluator = _make_user("ev_sa@t.br", "Ev", ProfileRole.ADVISOR)
        member = add_institutional_member(self.defense_future, CommitteeRole.PRESIDENT, evaluator, actor=self.admin)
        self.client.force_authenticate(user=evaluator)
        url = f"/api/v1/defenses/{self.defense_future.pk}/members/{member.pk}/evaluation"
        response = self.client.put(url, {"decision": "APPROVED"}, format="json")
        self.assertEqual(response.status_code, 400)


# ─── revert_defense_status (API level) ───────────────────────────────────────

@override_settings(CACHES=TEST_CACHES)
class RevertDefenseStatusApiTests(TestCase):
    """API tests for POST /api/v1/defenses/<id>/revert-status."""

    def setUp(self):
        self.client = APIClient()
        dept = Department.objects.create(name="CC-Revert")
        self.admin = _make_user("adm_rv@t.br", "Admin", ProfileRole.ADMIN)
        self.coord = _make_user("crd_rv@t.br", "Coord", ProfileRole.COORDINATOR)
        self.advisor = _make_user("adv_rv@t.br", "Adv", ProfileRole.ADVISOR)
        course = Course.objects.create(department=dept, coordinator=self.coord, name="Revert", code="RV1")
        thesis = Thesis.objects.create(course=course, advisor=self.advisor, title="T-RV", status=ThesisStatus.ADVISOR_APPROVED)
        # Schedule with past datetime so start_defense works (manual start requires SCHEDULED status)
        future = timezone.now() + timedelta(hours=2)
        self.defense = schedule_defense(thesis, scheduled_datetime=future, actor=self.admin)
        start_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()

    def _url(self, defense=None):
        d = defense or self.defense
        return f"/api/v1/defenses/{d.pk}/revert-status"

    def test_admin_reverts_in_progress_to_scheduled(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self._url(), {"reason": "Teste de reversão"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.SCHEDULED)

    def test_revert_without_reason_returns_400(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self._url(), {"reason": ""}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_coordinator_cannot_revert_returns_403(self):
        self.client.force_authenticate(user=self.coord)
        response = self.client.post(self._url(), {"reason": "Coord tentando reverter"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_advisor_cannot_revert_returns_403(self):
        self.client.force_authenticate(user=self.advisor)
        response = self.client.post(self._url(), {"reason": "Adv tentando reverter"}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_completed_defense_cannot_be_reverted(self):
        # Use REJECTED so close_defense doesn't call purge_thesis_versions (no file attached)
        from theses.defense_evaluation import submit_committee_evaluation
        from theses.defense_scheduling import close_defense
        evaluator = _make_user("ev_rv@t.br", "Ev-RV", ProfileRole.ADVISOR)
        member = add_institutional_member(self.defense, CommitteeRole.PRESIDENT, evaluator, actor=self.admin)
        record_evaluation_without_auto_close(member, "REJECTED")
        close_defense(self.defense, actor=self.admin)
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.COMPLETED)

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self._url(), {"reason": "Tentar reverter encerrada"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_cancelled_defense_reverts_to_previous_status(self):
        from theses.defense_scheduling import cancel_defense
        # Create a fresh SCHEDULED defense and cancel it
        dept = Department.objects.create(name="CC-Cancel-Revert")
        coord = _make_user("crd_cr@t.br", "CoordCR", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=dept, coordinator=coord, name="CR", code="CR1")
        advisor = _make_user("adv_cr@t.br", "AdvCR", ProfileRole.ADVISOR)
        thesis = Thesis.objects.create(course=course, advisor=advisor, title="T-CR", status=ThesisStatus.ADVISOR_APPROVED)
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        cancel_defense(defense, reason="Cancelado", actor=self.admin)
        defense.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.CANCELLED)
        self.assertEqual(defense.previous_status, DefenseStatus.SCHEDULED)

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self._url(defense), {"reason": "Reverter cancelamento"}, format="json")
        self.assertEqual(response.status_code, 200)
        defense.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.SCHEDULED)

    def test_cancelled_without_previous_status_falls_back_to_scheduled(self):
        dept = Department.objects.create(name="CC-Fallback-Revert")
        coord = _make_user("crd_fb@t.br", "CoordFB", ProfileRole.COORDINATOR)
        course = Course.objects.create(department=dept, coordinator=coord, name="FB", code="FB1")
        advisor = _make_user("adv_fb@t.br", "AdvFB", ProfileRole.ADVISOR)
        thesis = Thesis.objects.create(course=course, advisor=advisor, title="T-FB", status=ThesisStatus.ADVISOR_APPROVED)
        defense = schedule_defense(thesis, scheduled_datetime=timezone.now() + timedelta(days=7), actor=self.admin)
        # Simulate old cancelled record without previous_status
        Defense.objects.filter(pk=defense.pk).update(status=DefenseStatus.CANCELLED, previous_status="")
        defense.refresh_from_db()

        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self._url(defense), {"reason": "Fallback"}, format="json")
        self.assertEqual(response.status_code, 200)
        defense.refresh_from_db()
        self.assertEqual(defense.status, DefenseStatus.SCHEDULED)

    def test_reverted_with_past_datetime_does_not_auto_reopen(self):
        # Revert IN_PROGRESS → SCHEDULED; defense was scheduled with future time
        # but after revert auto_start_suspended must block re-opening even if we
        # manually set scheduled_datetime to past
        self.client.force_authenticate(user=self.admin)
        self.client.post(self._url(), {"reason": "Revert para teste"}, format="json")
        self.defense.refresh_from_db()
        self.assertEqual(self.defense.status, DefenseStatus.SCHEDULED)
        self.assertTrue(self.defense.auto_start_suspended)

        # Push scheduled_datetime to past without going through update_defense
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(hours=1))

        # GET detail must NOT auto-start because auto_start_suspended is True
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.SCHEDULED)

    def test_after_reschedule_auto_start_resumes(self):
        # Revert, reschedule with new future datetime (clears flag), then push to past → auto-starts
        self.client.force_authenticate(user=self.admin)
        self.client.post(self._url(), {"reason": "Revert"}, format="json")
        self.defense.refresh_from_db()

        new_future = (timezone.now() + timedelta(days=1)).isoformat()
        self.client.patch(f"/api/v1/defenses/{self.defense.pk}", {"scheduledDatetime": new_future}, format="json")
        self.defense.refresh_from_db()
        self.assertFalse(self.defense.auto_start_suspended)

        # Simulate time passing: set scheduled_datetime to past directly
        Defense.objects.filter(pk=self.defense.pk).update(scheduled_datetime=timezone.now() - timedelta(hours=1))

        # GET detail should now auto-start
        response = self.client.get(f"/api/v1/defenses/{self.defense.pk}")
        self.assertEqual(response.data["defense"]["status"], DefenseStatus.IN_PROGRESS)

    def test_revert_records_audit_log(self):
        self.client.force_authenticate(user=self.admin)
        self.client.post(self._url(), {"reason": "Auditoria"}, format="json")
        entry = AuditLog.objects.filter(
            entity_type="Defense",
            entity_id=str(self.defense.pk),
            metadata__event="STATUS_REVERTED_BY_ADMIN",
        ).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.metadata["from"], DefenseStatus.IN_PROGRESS)
        self.assertEqual(entry.metadata["to"], DefenseStatus.SCHEDULED)
