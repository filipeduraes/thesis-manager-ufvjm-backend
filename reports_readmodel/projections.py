"""
Project domain events into reporting facts.

Each handler is idempotent through ``ProcessedEvent`` (group ``reports``) and
updates only the row of the thesis named by the event.
"""

from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_datetime

from events.catalog import EventType
from events.envelope import EventEnvelope
from events.models import ProcessedEvent
from reports_readmodel.models import ReportFact
from theses.models import Defense, Thesis, ThesisStatus

SUPPORTED_EVENTS = frozenset(
    {
        EventType.THESIS_SUBMITTED,
        EventType.DEFENSE_COMPLETED,
        EventType.THESIS_DEFENSE_RESULT_RECORDED,
        EventType.THESIS_PUBLISHED,
    }
)

EXPECTED_KEYS: dict[str, frozenset[str]] = {
    EventType.THESIS_SUBMITTED: frozenset({"thesisId", "versionNumber", "submittedAt"}),
    EventType.DEFENSE_COMPLETED: frozenset({"defenseId", "thesisId", "result", "thesisNewStatus", "automatic"}),
    EventType.THESIS_DEFENSE_RESULT_RECORDED: frozenset({"thesisId", "result", "thesisNewStatus"}),
    EventType.THESIS_PUBLISHED: frozenset({"thesisId", "referenceCode", "publishedAt"}),
}


def _fact(thesis_id: str) -> ReportFact:
    """Return the fact of a thesis, loading identity data when needed."""
    fact, _ = ReportFact.objects.get_or_create(thesis_id=thesis_id)
    if not fact.course_id:
        thesis = Thesis.objects.using("default").filter(pk=thesis_id).select_related("course", "advisor").first()
        if thesis is not None:
            fact.course_id = thesis.course_id
            fact.course_name = thesis.course.name if thesis.course_id else ""
            fact.course_code = thesis.course.code if thesis.course_id else ""
            fact.advisor_id = thesis.advisor_id
            fact.advisor_name = thesis.advisor.name if thesis.advisor_id else ""
            fact.save(update_fields=["course_id", "course_name", "course_code", "advisor_id", "advisor_name"])
    return fact


def handle_event(envelope: EventEnvelope) -> str:
    """Apply one reporting event at most once."""
    if envelope.event_type not in SUPPORTED_EVENTS:
        return "SKIPPED"
    if set(envelope.payload) != EXPECTED_KEYS[envelope.event_type]:
        return "REJECTED"
    with transaction.atomic():
        try:
            ProcessedEvent.objects.create(consumer_group="reports", event_id=envelope.event_id)
        except IntegrityError:
            return "DUPLICATE"
        payload = envelope.payload
        fact = _fact(payload["thesisId"])
        if envelope.event_type == EventType.THESIS_SUBMITTED:
            submitted = parse_datetime(payload["submittedAt"])
            fact.submitted_at = submitted
        elif envelope.event_type == EventType.DEFENSE_COMPLETED:
            defense = Defense.objects.using("default").filter(pk=payload["defenseId"]).first()
            fact.defense_at = defense.scheduled_datetime if defense else None
            fact.result = payload["result"]
            fact.thesis_status = payload["thesisNewStatus"]
            thesis = Thesis.objects.using("default").filter(pk=fact.thesis_id).first()
            fact.defense_year = thesis.defense_year if thesis else (fact.defense_at.year if fact.defense_at else None)
        elif envelope.event_type == EventType.THESIS_DEFENSE_RESULT_RECORDED:
            thesis = Thesis.objects.using("default").filter(pk=fact.thesis_id).first()
            fact.result = payload["result"]
            fact.thesis_status = payload["thesisNewStatus"]
            fact.defense_at = thesis.defense_date if thesis and thesis.defense_date else fact.defense_at
            fact.defense_year = thesis.defense_year if thesis else fact.defense_year
        elif envelope.event_type == EventType.THESIS_PUBLISHED:
            thesis = Thesis.objects.using("default").filter(pk=fact.thesis_id).first()
            fact.is_published = True
            fact.thesis_status = ThesisStatus.PUBLISHED
            if thesis:
                fact.defense_year = thesis.defense_year
                fact.defense_at = thesis.defense_date if thesis.defense_date else fact.defense_at
        fact.save()
    return "PROCESSED"
