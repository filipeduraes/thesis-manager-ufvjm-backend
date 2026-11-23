"""
Normalized search index for theses.

The public repository searches a single denormalized, accent-free field built
from the thesis metadata and from the text extracted from its current PDF. A
single field keeps the query database-agnostic (it works on PostgreSQL and on
the SQLite test configuration) and avoids an external search service.
"""

from __future__ import annotations

import unicodedata

from django.db.models import QuerySet

from theses.models import Thesis

FIELD_LABELS = {
    "title": "titulo",
    "abstract": "resumo",
    "author": "autor",
    "advisor": "orientador",
    "course": "curso",
    "keyword": "palavrachave",
    "full_text": "textopdf",
}


def normalize_search_text(value: str) -> str:
    """
    Remove accents, fold case and collapse whitespace for search matching.

    Args:
        value: Raw text.

    Returns:
        str: Normalized text safe for accent-insensitive comparisons.
    """
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().split())


def search_term(value: str) -> str:
    """
    Normalize a user-supplied search term using the same rules as the index.

    Args:
        value: Raw user input.

    Returns:
        str: Normalized term.
    """
    return normalize_search_text(value)


def field_term(field: str, value: str) -> str:
    """
    Build the labeled fragment used to search one specific field.

    Args:
        field: Key from ``FIELD_LABELS``.
        value: Raw user input.

    Returns:
        str: Labeled normalized term.
    """
    label = FIELD_LABELS[field]
    return f"{label}::{normalize_search_text(value)}"


def build_search_text(thesis: Thesis) -> str:
    """
    Build the complete normalized search document of a thesis.

    Args:
        thesis: Thesis with authors, keywords, course and advisor loaded.

    Returns:
        str: Normalized document with one labeled segment per field.
    """
    values: list[tuple[str, str]] = [
        ("title", thesis.title),
        ("abstract", thesis.abstract),
    ]
    values.extend(("author", link.display_name) for link in thesis.authors.all())
    values.extend(("keyword", link.keyword.name) for link in thesis.keyword_links.all())
    if thesis.advisor_id:
        values.append(("advisor", thesis.advisor.name))
    if thesis.course_id:
        values.append(("course", thesis.course.name))
        values.append(("course", thesis.course.code))
    current = thesis.files.select_related("stored_file").filter(is_current=True).first()
    if current is not None and current.stored_file.extracted_text:
        values.append(("full_text", current.stored_file.extracted_text))
    parts = [f"{FIELD_LABELS[field]}::{normalize_search_text(value)}" for field, value in values if value]
    return " | ".join(parts)


def refresh_search_text(thesis: Thesis, fields: tuple[str, ...] | None = None) -> None:
    """
    Persist the normalized search document of a thesis.

    Args:
        thesis: Thesis to index.
        fields: Unused placeholder kept for call-site readability; the whole
            document is rebuilt so stale fragments cannot survive an edit.
    """
    document = build_search_text(thesis)
    Thesis.objects.filter(pk=thesis.pk).update(search_text=document)
    thesis.search_text = document


def refresh_search_text_for_files(stored_file_ids: QuerySet | list) -> None:
    """
    Reindex every thesis whose current version uses one of the given files.

    Args:
        stored_file_ids: Iterable or queryset of stored file identifiers.
    """
    theses = Thesis.objects.filter(files__is_current=True, files__stored_file_id__in=stored_file_ids).distinct()
    thesis: Thesis
    for thesis in theses:
        refresh_search_text(thesis)
