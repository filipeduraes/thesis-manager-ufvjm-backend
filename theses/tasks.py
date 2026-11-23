"""
Asynchronous-friendly helpers for thesis file processing.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache
from django.db import transaction
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone
from pypdf import PdfReader

from theses.events import record_thesis_text_extracted
from theses.models import ProcessingStatus, StoredFile, Thesis, ThesisFile
from theses.search_index import refresh_search_text_for_files
from theses.uploads import clear_upload_in_progress

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
    stored_file.extracted_text = text
    stored_file.extracted_at = timezone.now()
    stored_file.processing_status = ProcessingStatus.COMPLETED
    stored_file.save(update_fields=["extracted_text_path", "extracted_text", "extracted_at", "processing_status"])
    ThesisFile.objects.filter(stored_file=stored_file).update(text_extracted_at=stored_file.extracted_at)
    refresh_search_text_for_files([stored_file.pk])
    with transaction.atomic():
        for thesis in Thesis.objects.filter(files__stored_file=stored_file).distinct():
            record_thesis_text_extracted(thesis, stored_file)
    clear_upload_in_progress(stored_file.sha256_hex)
    return text


def extract_pending_thesis_text(limit: int = 10) -> dict[str, int]:
    """
    Process pending stored PDFs, marking failures without stopping the batch.

    Args:
        limit: Maximum number of files to process in this run.

    Returns:
        dict[str, int]: Counters for completed and failed files.
    """
    # SKIP LOCKED + mudança de status em transação curta: dois workers não
    # podem processar o mesmo PDF, e a extração (potencialmente lenta) roda
    # sem manter locks de banco.
    with transaction.atomic():
        claimed = list(
            StoredFile.objects.select_for_update(skip_locked=True)
            .filter(
                processing_status__in=[ProcessingStatus.PENDING, ProcessingStatus.RETRYING],
            )
            .order_by("uploaded_at")[:limit]
        )
        for stored_file in claimed:
            stored_file.processing_status = ProcessingStatus.PROCESSING
            stored_file.save(update_fields=["processing_status"])
    completed = 0
    failed = 0
    for stored_file in claimed:
        try:
            extract_stored_file_text(stored_file)
            completed += 1
        except Exception:
            failed += 1
            stored_file.processing_status = ProcessingStatus.FAILED
            stored_file.save(update_fields=["processing_status"])
            clear_upload_in_progress(stored_file.sha256_hex)
            logger.exception("Failed to extract text from stored file %s", stored_file.pk)
    return {"completed": completed, "failed": failed}
