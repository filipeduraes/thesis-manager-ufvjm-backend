"""
Redis cache of the public repository (search, detail, filters and courses).

Search pages use the key ``busca:<sha256>``, where the hash covers the
normalized filters, the page and a version token. Publishing, editing or
unpublishing a thesis rotates the token, so every cached page becomes
unreachable at once without scanning or deleting keys by prefix; the stale
entries simply expire with their TTL.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Callable
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.db import transaction

if TYPE_CHECKING:
    from theses.repository import PublicSearchFilters

logger = logging.getLogger(__name__)

SEARCH_KEY_PREFIX = "busca"
VERSION_KEY = "busca:versao"
DETAIL_KEY_PREFIX = "repositorio:detalhe"
FILTERS_KEY_PREFIX = "repositorio:filtros"
COURSES_KEY_PREFIX = "repositorio:cursos"


def cache_ttl_seconds() -> int:
    """
    Return the TTL of public repository entries.

    Returns:
        int: Seconds, from ``PUBLIC_REPOSITORY_CACHE_TTL_SECONDS``.
    """
    return int(getattr(settings, "PUBLIC_REPOSITORY_CACHE_TTL_SECONDS", 300))


def current_cache_version() -> str:
    """
    Return the version token embedded in every repository cache key.

    Returns:
        str: Current token, created on first use.
    """
    version = cache.get(VERSION_KEY)
    if version is None:
        # `add` não sobrescreve: dois processos que chegam juntos acabam com o mesmo token.
        cache.add(VERSION_KEY, uuid4().hex, None)
        version = cache.get(VERSION_KEY)
    return str(version)


def _rotate_version() -> None:
    """
    Replace the version token, invalidating every repository entry.
    """
    cache.set(VERSION_KEY, uuid4().hex, None)
    logger.info("Public repository cache invalidated")


def invalidate_public_repository_cache() -> None:
    """
    Invalidate the public repository cache after a change to published data.

    The token is rotated immediately and again after the commit: the second
    rotation discards pages that a concurrent request may have cached from the
    pre-commit state between the change and the commit. Rolled-back changes only
    cost an unnecessary invalidation.
    """
    _rotate_version()
    transaction.on_commit(_rotate_version)


def invalidate_public_repository_cache_after_projection() -> None:
    """
    Rotate the version after the asynchronous read model has been committed.

    The source-side invalidation happens before the projector writes, so a
    search between both events could cache stale projected data under the new
    version. This second rotation closes that window without registering an
    ``on_commit`` callback in the long-running consumer.
    """
    _rotate_version()


def search_cache_key(filters: PublicSearchFilters, page: str, page_size: str) -> str:
    """
    Build the ``busca:<hash_da_query>`` key of a search page.

    Args:
        filters: Validated search filters.
        page: Raw ``page`` parameter.
        page_size: Raw ``pageSize`` parameter.

    Returns:
        str: Cache key.
    """
    material = {"version": current_cache_version(), "filters": filters.cache_material(), "page": page, "pageSize": page_size}
    serialized = json.dumps(material, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return f"{SEARCH_KEY_PREFIX}:{digest}"


def detail_cache_key(thesis_id: str) -> str:
    """
    Build the cache key of a public thesis detail.

    Args:
        thesis_id: Thesis identifier.

    Returns:
        str: Cache key.
    """
    return f"{DETAIL_KEY_PREFIX}:{thesis_id}:{current_cache_version()}"


def filters_cache_key() -> str:
    """
    Build the cache key of the public filter options.

    Returns:
        str: Cache key.
    """
    return f"{FILTERS_KEY_PREFIX}:{current_cache_version()}"


def courses_cache_key(only_with_published: bool) -> str:
    """
    Build the cache key of the public course list.

    Args:
        only_with_published: Whether the list keeps only courses with published theses.

    Returns:
        str: Cache key.
    """
    scope = "publicados" if only_with_published else "todos"
    return f"{COURSES_KEY_PREFIX}:{scope}:{current_cache_version()}"


def cached_payload(key: str, build_payload: Callable[[], Any]) -> tuple[Any, bool]:
    """
    Return a cached payload, building and storing it on a miss.

    Args:
        key: Cache key.
        build_payload: Callable that computes the payload from the database.

    Returns:
        tuple[Any, bool]: Payload and whether it came from the cache.
    """
    cached = cache.get(key)
    if cached is not None:
        return cached, True
    payload = build_payload()
    cache.set(key, payload, cache_ttl_seconds())
    return payload, False
