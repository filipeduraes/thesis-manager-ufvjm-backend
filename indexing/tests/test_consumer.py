"""
Tests of the indexing consumer: extraction, index refresh and idempotency.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import tempfile
from datetime import date
from uuid import uuid4

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.test import TestCase, override_settings
from reportlab.pdfgen import canvas

from academic.models import Course, Department
from accounts.models import ProfileRole
from events.envelope import EventEnvelope
from events.models import ProcessedEvent
from indexing.consumer import CONSUMER_GROUP, HandleOutcome, handle_event
from theses.events import record_thesis_text_extraction_requested
from theses.models import ProcessingStatus, StoredFile, Thesis, ThesisAuthor, ThesisFile, ThesisStatus
from theses.repository import PublicSearchFilters, search_public_theses
from theses.tests.test_api import create_user

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


def pdf_with_text(text: str) -> bytes:
    """
    Build a one-page PDF containing visible text.

    Args:
        text: Word written on the page.

    Returns:
        bytes: PDF content.
    """
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.drawString(72, 720, text)
    pdf.save()
    return buffer.getvalue()


@override_settings(CACHES=TEST_CACHES)
class IndexingConsumerTests(TestCase):
    """The consumer extracts text once and makes it searchable."""

    def setUp(self) -> None:
        cache.clear()
        self.media_root = tempfile.mkdtemp(prefix="indexing-media-")
        media_override = override_settings(MEDIA_ROOT=self.media_root)
        media_override.enable()
        self.addCleanup(media_override.disable)
        self.addCleanup(shutil.rmtree, self.media_root, True)
        department = Department.objects.create(name="Computação")
        course = Course.objects.create(department=department, name="Ciência da Computação", code="CC")
        advisor = create_user("orientador@ufvjm.edu.br", "Orientador", ProfileRole.ADVISOR)
        student = create_user("aluno@ufvjm.edu.br", "Aluno", ProfileRole.STUDENT)
        self.student = student
        self.thesis = Thesis.objects.create(
            course=course,
            advisor=advisor,
            title="Sistema de indexação",
            abstract="Resumo sobre busca.",
            status=ThesisStatus.PUBLISHED,
            is_public=True,
            published_at="2026-01-01T12:00:00Z",
            defense_date=date(2026, 1, 1),
            defense_year=2026,
        )
        ThesisAuthor.objects.create(thesis=self.thesis, author=student, position=1, is_main_author=True)
        content = pdf_with_text("PalavraExotica")
        stored_name = default_storage.save(f"theses/test/{uuid4().hex}.pdf", ContentFile(content))
        self.stored_file = StoredFile.objects.create(
            original_filename="indexacao.pdf",
            stored_filename=stored_name.rsplit("/", 1)[-1],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).digest(),
            storage_path=stored_name,
        )
        ThesisFile.objects.create(thesis=self.thesis, stored_file=self.stored_file, version_number=1, is_current=True, uploaded_by=student)

    def envelope(self) -> EventEnvelope:
        """Build a valid indexing event for the test stored file."""
        with transaction.atomic():
            event = record_thesis_text_extraction_requested(self.thesis, self.stored_file, trigger="UPLOAD")
        return EventEnvelope.from_outbox(event)

    def test_extracts_text_and_refreshes_search_index(self) -> None:
        outcome = handle_event(self.envelope())
        self.assertEqual(outcome, HandleOutcome.PROCESSED)
        self.stored_file.refresh_from_db()
        self.assertEqual(self.stored_file.processing_status, ProcessingStatus.COMPLETED)
        self.assertIn("PalavraExotica", self.stored_file.extracted_text)
        self.thesis.refresh_from_db()
        self.assertIn("palavraexotica", self.thesis.search_text)
        filters = PublicSearchFilters(query="PalavraExotica")
        self.assertEqual(list(search_public_theses(filters).values_list("pk", flat=True)), [self.thesis.pk])

    def test_duplicate_event_does_not_reindex(self) -> None:
        envelope = self.envelope()
        self.assertEqual(handle_event(envelope), HandleOutcome.PROCESSED)
        self.assertEqual(handle_event(envelope), HandleOutcome.DUPLICATE)
        self.assertEqual(ProcessedEvent.objects.filter(consumer_group=CONSUMER_GROUP).count(), 1)

    def test_unknown_payload_is_rejected(self) -> None:
        envelope = self.envelope()
        payload = {**envelope.payload, "extra": 1}
        unknown = EventEnvelope(**{**envelope.__dict__, "payload": payload})
        self.assertEqual(handle_event(unknown), HandleOutcome.REJECTED)

    def test_event_of_another_consumer_is_skipped(self) -> None:
        envelope = self.envelope()
        other = EventEnvelope(**{**envelope.__dict__, "event_type": "thesis.submitted", "payload": {}})
        self.assertEqual(handle_event(other), HandleOutcome.SKIPPED)
