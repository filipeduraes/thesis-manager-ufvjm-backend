"""Structural PDF validation tests (written before the implementation)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from theses.services import _validate_pdf_upload

VALID_PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def upload(content: bytes) -> SimpleUploadedFile:
    """Build a multipart upload from raw bytes."""
    return SimpleUploadedFile("arquivo.pdf", content, content_type="application/pdf")


class PdfStructuralValidationTests(SimpleTestCase):
    """The upload must reject files that only mimic the PDF signature."""

    def test_valid_pdf_is_accepted(self) -> None:
        digest, size = _validate_pdf_upload(upload(VALID_PDF), "THESIS_MAX_PDF_SIZE_MB", 50)
        self.assertEqual(size, len(VALID_PDF))
        self.assertEqual(len(digest), 32)

    def test_missing_eof_marker_is_rejected(self) -> None:
        truncated = VALID_PDF.replace(b"%%EOF", b"")
        with self.assertRaises(ValidationError) as context:
            _validate_pdf_upload(upload(truncated), "THESIS_MAX_PDF_SIZE_MB", 50)
        self.assertIn("PDF", str(context.exception))

    def test_header_without_pdf_body_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            _validate_pdf_upload(upload(b"%PDF-1.4\nconteudo qualquer"), "THESIS_MAX_PDF_SIZE_MB", 50)

    def test_trailing_whitespace_after_eof_is_allowed(self) -> None:
        _validate_pdf_upload(upload(VALID_PDF.rstrip() + b"\n\n "), "THESIS_MAX_PDF_SIZE_MB", 50)

    def test_trailing_comment_after_eof_is_allowed(self) -> None:
        # O helper E2E do frontend acrescenta um comentário único após o %%EOF.
        _validate_pdf_upload(upload(VALID_PDF + b"\n% e2e-marker\n"), "THESIS_MAX_PDF_SIZE_MB", 50)
