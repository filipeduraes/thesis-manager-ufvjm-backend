"""
Outbox events emitted by the thesis and defense workflows.

Each helper builds the catalog payload (identifiers and codes only) and records it
in the caller's transaction, so services need a single call per transition.
"""

from __future__ import annotations

from datetime import datetime

from events.catalog import EventType
from events.models import OutboxEvent
from events.outbox import AggregateRef, record_event
from theses.models import Defense, DefenseMember, StoredFile, Thesis


def _isoformat(value: datetime | None) -> str | None:
    """
    Format an optional timezone-aware datetime for an event payload.

    Args:
        value: Datetime to format.

    Returns:
        str | None: ISO 8601 string, or None when absent.
    """
    return value.isoformat() if value is not None else None


def _thesis_aggregate(thesis: Thesis) -> AggregateRef:
    """
    Build the aggregate reference of a thesis.

    Args:
        thesis: Thesis instance.

    Returns:
        AggregateRef: Thesis aggregate reference.
    """
    return AggregateRef("Thesis", thesis.pk)


def _defense_aggregate(defense: Defense) -> AggregateRef:
    """
    Build the aggregate reference of a defense.

    Args:
        defense: Defense instance.

    Returns:
        AggregateRef: Defense aggregate reference.
    """
    return AggregateRef("Defense", defense.pk)


def record_thesis_submitted(thesis: Thesis, version_number: int) -> OutboxEvent:
    """
    Record ``thesis.submitted`` after the student submits a version for advisor review.

    Args:
        thesis: Submitted thesis (with ``submitted_at`` already set).
        version_number: Number of the submitted file version.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"thesisId": str(thesis.pk), "versionNumber": version_number, "submittedAt": _isoformat(thesis.submitted_at)}
    return record_event(EventType.THESIS_SUBMITTED, _thesis_aggregate(thesis), payload)


def record_thesis_text_extraction_requested(thesis: Thesis, stored_file: StoredFile, trigger: str) -> OutboxEvent:
    """
    Record ``thesis.text_extraction_requested`` for asynchronous PDF indexing.

    Args:
        thesis: Thesis that owns the file.
        stored_file: Stored PDF awaiting text extraction.
        trigger: Code of the workflow step that requested it.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"thesisId": str(thesis.pk), "storedFileId": str(stored_file.pk), "trigger": trigger}
    return record_event(EventType.THESIS_TEXT_EXTRACTION_REQUESTED, _thesis_aggregate(thesis), payload)


def record_defense_scheduled(defense: Defense) -> OutboxEvent:
    """
    Record ``defense.scheduled`` after a defense is created for a thesis.

    Location and meeting link are left out on purpose; consumers read them from the database.

    Args:
        defense: Newly scheduled defense.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {
        "defenseId": str(defense.pk),
        "thesisId": str(defense.thesis_id),
        "scheduledDatetime": _isoformat(defense.scheduled_datetime),
        "durationMinutes": defense.duration_minutes,
        "modality": defense.modality,
    }
    return record_event(EventType.DEFENSE_SCHEDULED, _defense_aggregate(defense), payload)


def record_defense_member_invited(defense: Defense, member: DefenseMember) -> OutboxEvent:
    """
    Record ``defense.member_invited`` when a member joins a scheduled defense.

    Args:
        defense: Defense the member joined.
        member: Created committee membership.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {
        "defenseId": str(defense.pk),
        "thesisId": str(defense.thesis_id),
        "memberId": str(member.pk),
        "invitationId": str(member.external_invitation_id) if member.external_invitation_id else None,
    }
    return record_event(EventType.DEFENSE_MEMBER_INVITED, _defense_aggregate(defense), payload)


def record_defense_evaluation_recorded(defense: Defense, member: DefenseMember, is_update: bool) -> OutboxEvent:
    """
    Record ``defense.evaluation_recorded`` after a committee member saves an evaluation.

    The decision, score and notes are not included: they stay confidential until
    the defense is closed.

    Args:
        defense: Defense in progress.
        member: Member who evaluated.
        is_update: Whether an existing evaluation was replaced.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"defenseId": str(defense.pk), "thesisId": str(defense.thesis_id), "memberId": str(member.pk), "isUpdate": is_update}
    return record_event(EventType.DEFENSE_EVALUATION_RECORDED, _defense_aggregate(defense), payload)


def record_defense_completed(defense: Defense, result: str, thesis_new_status: str, automatic: bool) -> OutboxEvent:
    """
    Record ``defense.completed`` after the committee result is computed and the defense closed.

    Args:
        defense: Completed defense.
        result: DefenseResult code.
        thesis_new_status: Thesis status after the close.
        automatic: Whether the close was triggered by the last evaluation.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"defenseId": str(defense.pk), "thesisId": str(defense.thesis_id), "result": result, "thesisNewStatus": thesis_new_status, "automatic": automatic}
    return record_event(EventType.DEFENSE_COMPLETED, _defense_aggregate(defense), payload)


def record_thesis_defense_result_recorded(thesis: Thesis, result: str, thesis_new_status: str) -> OutboxEvent:
    """
    Record ``thesis.defense_result_recorded`` when a manager records the result without a ``Defense``.

    Kept separate from ``defense.completed`` because this path has no defense identifier.

    Args:
        thesis: Thesis whose result was recorded.
        result: DefenseResult code.
        thesis_new_status: Thesis status after the change.

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"thesisId": str(thesis.pk), "result": result, "thesisNewStatus": thesis_new_status}
    return record_event(EventType.THESIS_DEFENSE_RESULT_RECORDED, _thesis_aggregate(thesis), payload)


def record_thesis_published(thesis: Thesis) -> OutboxEvent:
    """
    Record ``thesis.published`` after the thesis enters the public repository.

    Args:
        thesis: Published thesis (with ``published_at`` already set).

    Returns:
        OutboxEvent: Recorded event.
    """
    payload = {"thesisId": str(thesis.pk), "referenceCode": thesis.reference_code, "publishedAt": _isoformat(thesis.published_at)}
    return record_event(EventType.THESIS_PUBLISHED, _thesis_aggregate(thesis), payload)
