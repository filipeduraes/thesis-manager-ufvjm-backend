"""
Tests for the protection of published theses: no deletion, only unpublication.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import Collector

from accounts.models import AuditAction, AuditLog, ProfileRole
from theses.models import Thesis, ThesisStatus
from theses.services import PublicationTrigger, publish_if_ready
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user


class ProtectionTestCase(RepositoryTestCase):
    """Fixtures: admin, coordinators of each course and a published thesis."""

    def setUp(self) -> None:
        super().setUp()
        self.admin = create_user("admin@ufvjm.edu.br", "Administração", ProfileRole.ADMIN)
        self.coordinator = create_user("coord.si@ufvjm.edu.br", "Coordenação SI", ProfileRole.COORDINATOR)
        self.other_coordinator = create_user("coord.cc@ufvjm.edu.br", "Coordenação CC", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])
        self.other_course.coordinator_email = self.other_coordinator.email
        self.other_course.save(update_fields=["coordinator_email"])
        self.published = create_thesis(ThesisSpec(title="Publicada protegida", course=self.course, advisor=self.advisor, authors=[self.student], keywords=["Protegida"]))


class PublishedDeletionTests(ProtectionTestCase):
    """No write path deletes a published thesis."""

    def test_model_and_queryset_delete_are_refused(self) -> None:
        with self.assertRaises(ValidationError):
            self.published.delete()
        with self.assertRaises(ValidationError):
            Thesis.objects.filter(pk=self.published.pk).delete()
        self.assertTrue(Thesis.objects.filter(pk=self.published.pk).exists())

    def test_cascade_deletion_path_is_refused_by_signal(self) -> None:
        collector = Collector(using="default")
        collector.collect([self.published])
        with self.assertRaises(ValidationError), transaction.atomic():
            collector.delete()
        self.assertTrue(Thesis.objects.filter(pk=self.published.pk).exists())

    def test_api_delete_is_refused_even_for_admin(self) -> None:
        for user in (self.admin, self.coordinator):
            self.client.force_authenticate(user=user)
            response = self.client.delete(f"/api/v1/theses/{self.published.pk}")
            self.assertEqual(response.status_code, 400, user.email)
            self.assertIn("despublicação", response.data["detail"])
        self.published.refresh_from_db()
        self.assertIsNone(self.published.deleted_at)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{self.published.pk}").status_code, 200)

    def test_unpublished_thesis_stays_protected(self) -> None:
        self.client.force_authenticate(user=self.admin)
        self.client.post(f"/api/v1/theses/{self.published.pk}/unpublish", {"reason": "Pedido do autor."}, format="json")
        self.assertEqual(self.client.delete(f"/api/v1/theses/{self.published.pk}").status_code, 400)
        with self.assertRaises(ValidationError):
            Thesis.objects.filter(pk=self.published.pk).delete()

    def test_never_published_thesis_is_soft_deleted_by_course_manager(self) -> None:
        draft = create_thesis(ThesisSpec(title="Rascunho descartável", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.DRAFT))
        self.client.force_authenticate(user=self.coordinator)
        response = self.client.delete(f"/api/v1/theses/{draft.pk}")
        self.assertEqual(response.status_code, 204)
        draft.refresh_from_db()
        self.assertIsNotNone(draft.deleted_at)
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DELETE, entity_id=str(draft.pk)).exists())
        self.assertEqual(self.client.get(f"/api/v1/theses/{draft.pk}").status_code, 404)

    def test_delete_permissions(self) -> None:
        draft = create_thesis(ThesisSpec(title="Rascunho", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.DRAFT))
        for user in (self.other_student, self.advisor):
            self.client.force_authenticate(user=user)
            self.assertEqual(self.client.delete(f"/api/v1/theses/{draft.pk}").status_code, 403, user.email)
        self.client.force_authenticate(user=self.other_coordinator)
        self.assertEqual(self.client.delete(f"/api/v1/theses/{draft.pk}").status_code, 404)
        self.client.force_authenticate(user=None)
        self.assertIn(self.client.delete(f"/api/v1/theses/{draft.pk}").status_code, {401, 403})
        draft.refresh_from_db()
        self.assertIsNone(draft.deleted_at)


class UnpublishTests(ProtectionTestCase):
    """Unpublication requires a reason, is recorded and leaves the repository at once."""

    def unpublish(self, user, thesis: Thesis, reason: str = "Termo de autorização revogado pelo autor."):
        self.client.force_authenticate(user=user)
        return self.client.post(f"/api/v1/theses/{thesis.pk}/unpublish", {"reason": reason}, format="json")

    def test_coordinator_unpublishes_with_reason(self) -> None:
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 1)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.unpublish(self.coordinator, self.published)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["thesis"]["status"], ThesisStatus.ARCHIVED)
        self.published.refresh_from_db()
        self.assertFalse(self.published.is_public)
        self.assertIsNotNone(self.published.published_at)
        entry = self.published.status_history.get(new_status=ThesisStatus.ARCHIVED)
        self.assertEqual(entry.changed_by, self.coordinator)
        self.assertEqual(entry.reason, "Termo de autorização revogado pelo autor.")
        self.assertEqual(entry.metadata["event"], "UNPUBLISHED")
        audit = AuditLog.objects.filter(entity_id=str(self.published.pk), metadata__event="UNPUBLISHED").get()
        self.assertEqual(audit.actor_user, self.coordinator)
        self.assertEqual(audit.changes["isPublic"], {"from": True, "to": False})
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get("/api/v1/public/theses").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{self.published.pk}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/v1/public/theses/{self.published.pk}/download").status_code, 404)
        self.assertEqual(self.client.get("/api/v1/public/theses/filters").data["keywords"], [])

    def test_admin_can_unpublish_any_course(self) -> None:
        other = create_thesis(ThesisSpec(title="Outro curso", course=self.other_course, advisor=self.other_advisor, authors=[self.other_student]))
        self.assertEqual(self.unpublish(self.admin, other).status_code, 200)

    def test_reason_is_required(self) -> None:
        response = self.unpublish(self.coordinator, self.published, reason="   ")
        self.assertEqual(response.status_code, 400)
        self.published.refresh_from_db()
        self.assertTrue(self.published.is_public)

    def test_only_published_theses_can_be_unpublished(self) -> None:
        approved = create_thesis(ThesisSpec(title="Aprovada", course=self.course, advisor=self.advisor, authors=[self.other_student], status=ThesisStatus.APPROVED))
        self.assertEqual(self.unpublish(self.coordinator, approved).status_code, 400)

    def test_unpublish_permissions(self) -> None:
        self.assertEqual(self.unpublish(self.other_coordinator, self.published).status_code, 404)
        self.assertEqual(self.unpublish(self.advisor, self.published).status_code, 403)
        self.assertEqual(self.unpublish(self.student, self.published).status_code, 403)
        self.published.refresh_from_db()
        self.assertTrue(self.published.is_public)

    def test_archived_thesis_is_not_republished_automatically(self) -> None:
        self.unpublish(self.admin, self.published)
        self.assertFalse(publish_if_ready(self.published, trigger=PublicationTrigger.AUTHORIZATION_VALIDATED))
        self.published.refresh_from_db()
        self.assertEqual(self.published.status, ThesisStatus.ARCHIVED)
