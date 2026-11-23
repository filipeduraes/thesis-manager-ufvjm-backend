"""
The validated publication authorization must be intact before publication.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.files.storage import default_storage
from django.test import override_settings

from accounts.models import ProfileRole
from theses.models import DocumentType, ThesisStatus
from theses.services import (
    PublicationTrigger,
    publish_if_ready,
    publish_thesis,
    upload_thesis_document,
    validate_thesis_document,
    verify_publication_authorization,
)
from theses.tests.repository_helpers import RepositoryTestCase, ThesisSpec, create_thesis
from theses.tests.test_api import create_user, pdf_upload


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class PublicationAuthorizationIntegrityTests(RepositoryTestCase):
    """A tampered authorization term blocks automatic and manual publication."""

    def setUp(self) -> None:
        super().setUp()
        self.coordinator = create_user("coord@ufvjm.edu.br", "Coordenação", ProfileRole.COORDINATOR)
        self.course.coordinator = self.coordinator
        self.course.save(update_fields=["coordinator"])
        self.thesis = create_thesis(
            ThesisSpec(
                title="Termo íntegro",
                course=self.course,
                advisor=self.advisor,
                authors=[self.student],
                keywords=["Publicação"],
                status=ThesisStatus.ADVISOR_APPROVED,
            )
        )
        self.authorization = upload_thesis_document(
            self.student,
            self.thesis,
            DocumentType.PUBLICATION_AUTHORIZATION,
            pdf_upload("termo-assinado.pdf"),
        )
        validate_thesis_document(self.coordinator, self.thesis, self.authorization)
        # Publica-se somente depois da adulteração: a validação do termo dispara
        # publish_if_ready, que não deve encontrar a monografia apta ainda.
        self.thesis.status = ThesisStatus.APPROVED
        self.thesis.save(update_fields=["status"])

    def tamper_authorization(self) -> None:
        path = self.authorization.stored_file.storage_path
        with default_storage.open(path, "wb") as destination:
            destination.write(b"%PDF-1.4\nconteudo alterado\n")

    def test_intact_authorization_allows_publication(self) -> None:
        self.assertTrue(verify_publication_authorization(self.thesis))
        self.assertTrue(publish_if_ready(self.thesis, trigger=PublicationTrigger.AUTHORIZATION_VALIDATED))
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.PUBLISHED)

    def test_tampered_authorization_blocks_automatic_publication(self) -> None:
        self.tamper_authorization()
        self.assertFalse(verify_publication_authorization(self.thesis))
        self.assertFalse(publish_if_ready(self.thesis, trigger=PublicationTrigger.AUTHORIZATION_VALIDATED))
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED)

    def test_tampered_authorization_blocks_manual_publication(self) -> None:
        self.tamper_authorization()
        with self.assertRaises(ValidationError):
            publish_thesis(self.coordinator, self.thesis)
        self.thesis.refresh_from_db()
        self.assertEqual(self.thesis.status, ThesisStatus.APPROVED)
