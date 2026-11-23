"""
Project published theses into the repository service's own database.

The projector loads the aggregate from the transactional database and writes
only public, denormalized data to the repository read model. Replaying an event
is safe: every projection is a full upsert of the affected thesis.
"""

from __future__ import annotations

from django.db import transaction

from accounts.concurrency import database_for_model
from events.catalog import EventType, is_known_event
from events.envelope import EventEnvelope
from repository_readmodel.models import PublicAdvisor, PublicCourse, PublicKeyword, PublicThesis
from theses.models import ProcessingStatus, Thesis, ThesisStatus
from theses.repository_cache import invalidate_public_repository_cache_after_projection
from theses.search_index import build_search_text


def project_thesis(thesis_id: str) -> None:
    """
    Replace the projection of one thesis with its current public state.

    Args:
        thesis_id: Identifier from the event payload.
    """
    thesis = Thesis.objects.using("default").filter(pk=thesis_id).first()
    if thesis is None or thesis.status != ThesisStatus.PUBLISHED or not thesis.is_public or thesis.deleted_at is not None:
        PublicThesis.objects.filter(pk=thesis_id).delete()
        return
    authors = [link.display_name for link in thesis.authors.all()]
    keywords = [link.keyword.name for link in thesis.keyword_links.all()]
    files = list(thesis.files.select_related("stored_file").order_by("version_number"))
    current = next((item for item in files if item.is_current), None)
    course = None
    if thesis.course_id:
        course, _ = PublicCourse.objects.update_or_create(
            pk=thesis.course_id,
            defaults={"name": thesis.course_name, "code": thesis.course_code},
        )
    advisor = None
    if thesis.advisor_id:
        advisor, _ = PublicAdvisor.objects.update_or_create(pk=thesis.advisor_id, defaults={"name": thesis.advisor_name})
    PublicThesis.objects.update_or_create(
        pk=thesis.pk,
        defaults={
            "reference_code": thesis.reference_code,
            "title": thesis.title,
            "abstract": thesis.abstract,
            "course": course,
            "advisor": advisor,
            "author_names": authors,
            "keywords": keywords,
            "defense_date": thesis.defense_date,
            "defense_year": thesis.defense_year,
            "published_at": thesis.published_at,
            "search_text": build_search_text(thesis),
            "pdf_sha256": current.stored_file.sha256_hex if current else "",
            "pdf_size_bytes": current.stored_file.size_bytes if current else 0,
            "pdf_version": current.version_number if current else 0,
            "file_versions": [
                {"versionNumber": item.version_number, "isCurrent": item.is_current, "sha256": item.stored_file.sha256_hex, "sizeBytes": item.stored_file.size_bytes}
                for item in files
            ],
            "origin": thesis.origin,
        },
    )
    for keyword_name in keywords:
        PublicKeyword.objects.update_or_create(
            name=keyword_name,
            defaults={"value": " ".join(keyword_name.casefold().split())},
        )
    counts: dict[str, int] = {}
    for values in PublicThesis.objects.values_list("keywords", flat=True):
        for name in values:
            counts[name] = counts.get(name, 0) + 1
    for keyword in PublicKeyword.objects.all():
        count = counts.get(keyword.name, 0)
        if keyword.published_count != count:
            keyword.published_count = count
            keyword.save(update_fields=["published_count"])


SUPPORTED_EVENTS = frozenset(
    {
        EventType.THESIS_PUBLISHED,
        EventType.THESIS_REPOSITORY_UPDATED,
        EventType.THESIS_TEXT_EXTRACTED,
    }
)


def handle_event(envelope: EventEnvelope) -> str:
    """
    Apply one repository event, at most once.

    Args:
        envelope: Decoded event.

    Returns:
        str: ``PROCESSED``, ``DUPLICATE``, ``SKIPPED`` or ``REJECTED``.
    """
    if envelope.event_type not in SUPPORTED_EVENTS:
        return "SKIPPED"
    if not is_known_event(envelope.event_type, envelope.event_version):
        return "REJECTED"
    expected = {"thesisId"} if envelope.event_type == EventType.THESIS_TEXT_EXTRACTED else {"thesisId", "trigger"} if envelope.event_type == EventType.THESIS_REPOSITORY_UPDATED else {"thesisId", "referenceCode", "publishedAt"}
    if set(envelope.payload) != expected:
        return "REJECTED"
    from events.models import ProcessedEvent
    from django.db import IntegrityError

    marker_db = database_for_model(ProcessedEvent)
    if ProcessedEvent.objects.using(marker_db).filter(consumer_group="repository", event_id=envelope.event_id).exists():
        return "DUPLICATE"
    with transaction.atomic(using=database_for_model(PublicThesis)):
        project_thesis(envelope.payload["thesisId"])
    invalidate_public_repository_cache_after_projection()
    try:
        with transaction.atomic(using=marker_db):
            ProcessedEvent.objects.using(marker_db).create(consumer_group="repository", event_id=envelope.event_id)
    except IntegrityError:
        return "DUPLICATE"
    return "PROCESSED"
