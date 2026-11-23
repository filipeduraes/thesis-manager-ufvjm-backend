"""
Redis-backed tracking of PDF uploads whose text extraction is still pending.

The key is derived from the SHA-256 of the file so the same document cannot be
re-sent while a worker is processing it. The cache backend is Redis in
production and the in-memory backend in the isolated test configuration.
"""

from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

UPLOAD_CACHE_PREFIX = "upload_em_andamento"
UPLOAD_CACHE_TIMEOUT_SECONDS = 60 * 60


def upload_cache_key(sha256_hex: str) -> str:
    """
    Build the cache key for an in-progress upload.

    Args:
        sha256_hex: Hexadecimal SHA-256 of the uploaded PDF.

    Returns:
        str: Namespaced cache key.
    """
    return f"{UPLOAD_CACHE_PREFIX}:{sha256_hex}"


def mark_upload_in_progress(sha256_hex: str) -> None:
    """
    Mark a document as being processed.

    Args:
        sha256_hex: Hexadecimal SHA-256 of the uploaded PDF.
    """
    timeout = getattr(settings, "UPLOAD_TRACKING_TTL_SECONDS", UPLOAD_CACHE_TIMEOUT_SECONDS)
    cache.set(upload_cache_key(sha256_hex), "processing", timeout=timeout)


def upload_in_progress(sha256_hex: str) -> bool:
    """
    Tell whether a document is already being processed.

    Args:
        sha256_hex: Hexadecimal SHA-256 of the uploaded PDF.

    Returns:
        bool: True when another upload/extraction is still running.
    """
    return cache.get(upload_cache_key(sha256_hex)) is not None


def clear_upload_in_progress(sha256_hex: str) -> None:
    """
    Release the in-progress marker after extraction succeeds or fails.

    Args:
        sha256_hex: Hexadecimal SHA-256 of the uploaded PDF.
    """
    cache.delete(upload_cache_key(sha256_hex))
