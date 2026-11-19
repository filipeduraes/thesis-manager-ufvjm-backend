"""
Catalog of domain events: types, contract versions and allowed payload keys.

This module is the single source of truth for the event contract shared by the
producers (thesis and defense services) and the consumers (notifications).
"""

from __future__ import annotations

from django.db import models

EVENT_VERSION_1_0 = "1.0"


class EventType(models.TextChoices):
    """
    Domain event types; the value is also used as the AMQP routing key.
    """

    THESIS_SUBMITTED = "thesis.submitted", "Monografia submetida"
    DEFENSE_SCHEDULED = "defense.scheduled", "Banca agendada"
    DEFENSE_EVALUATION_RECORDED = "defense.evaluation_recorded", "Avaliação registrada"
    DEFENSE_COMPLETED = "defense.completed", "Banca concluída"
    THESIS_DEFENSE_RESULT_RECORDED = "thesis.defense_result_recorded", "Resultado da banca registrado"
    THESIS_PUBLISHED = "thesis.published", "Monografia publicada"


# Payloads levam apenas identificadores e códigos; nomes, e-mails, notas, local e
# link da banca são lidos do banco pelo consumidor. O conjunto de chaves é exato.
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    EventType.THESIS_SUBMITTED: frozenset({"thesisId", "versionNumber", "submittedAt"}),
    EventType.DEFENSE_SCHEDULED: frozenset({"defenseId", "thesisId", "scheduledDatetime", "durationMinutes", "modality"}),
    EventType.DEFENSE_EVALUATION_RECORDED: frozenset({"defenseId", "thesisId", "memberId", "isUpdate"}),
    EventType.DEFENSE_COMPLETED: frozenset({"defenseId", "thesisId", "result", "thesisNewStatus", "automatic"}),
    EventType.THESIS_DEFENSE_RESULT_RECORDED: frozenset({"thesisId", "result", "thesisNewStatus"}),
    EventType.THESIS_PUBLISHED: frozenset({"thesisId", "referenceCode", "publishedAt"}),
}

CURRENT_VERSIONS: dict[str, str] = {event_type: EVENT_VERSION_1_0 for event_type in EventType.values}


def is_known_event(event_type: str, event_version: str) -> bool:
    """
    Tell whether an event type and contract version belong to the catalog.

    Args:
        event_type: Event type code, e.g. ``"defense.completed"``.
        event_version: Contract version, e.g. ``"1.0"``.

    Returns:
        bool: True when the pair is part of the current catalog.
    """
    return CURRENT_VERSIONS.get(event_type) == event_version
