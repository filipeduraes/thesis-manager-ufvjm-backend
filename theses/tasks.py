"""
Asynchronous-friendly helpers for thesis file processing.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from pypdf import PdfReader

from theses.models import ProcessingStatus, StoredFile, ThesisFile

logger = logging.getLogger(__name__)


def extract_stored_file_text(stored_file: StoredFile) -> str:
    """
    Extract the text of a stored PDF and persist it as a derived text file.

    Args:
        stored_file: Stored PDF to process.

    Returns:
        str: Extracted text.

    Raises:
        Exception: Propagates storage or PDF parsing failures for the caller to handle.
    """
    with default_storage.open(stored_file.storage_path, "rb") as source:
        reader = PdfReader(source)
        pages = [page.extract_text() or "" for page in reader.pages]
    text = "\n\n".join(pages)
    text_name = f"theses/extracted/{stored_file.sha256_hex}.txt"
    saved_name = default_storage.save(text_name, ContentFile(text.encode("utf-8")))
    stored_file.extracted_text_path = saved_name
    stored_file.extracted_at = timezone.now()
    stored_file.processing_status = ProcessingStatus.COMPLETED
    stored_file.save(update_fields=["extracted_text_path", "extracted_at", "processing_status"])
    ThesisFile.objects.filter(stored_file=stored_file).update(text_extracted_at=stored_file.extracted_at)
    return text


def extract_pending_thesis_text(limit: int = 10) -> dict[str, int]:
    """
    Process pending stored PDFs, marking failures without stopping the batch.

    Args:
        limit: Maximum number of files to process in this run.

    Returns:
        dict[str, int]: Counters for completed and failed files.
    """
    pending = StoredFile.objects.filter(
        processing_status__in=[ProcessingStatus.PENDING, ProcessingStatus.RETRYING],
    ).order_by("uploaded_at")[:limit]
    completed = 0
    failed = 0
    for stored_file in pending:
        stored_file.processing_status = ProcessingStatus.PROCESSING
        stored_file.save(update_fields=["processing_status"])
        try:
            extract_stored_file_text(stored_file)
            completed += 1
        except Exception:
            failed += 1
            stored_file.processing_status = ProcessingStatus.FAILED
            stored_file.save(update_fields=["processing_status"])
            logger.exception("Failed to extract text from stored file %s", stored_file.pk)
    return {"completed": completed, "failed": failed}
