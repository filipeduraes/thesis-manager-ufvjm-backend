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
    THESIS_TEXT_EXTRACTION_REQUESTED = "thesis.text_extraction_requested", "Extração de texto solicitada"
    THESIS_TEXT_EXTRACTED = "thesis.text_extracted", "Texto extraído"
    THESIS_REPOSITORY_UPDATED = "thesis.repository_updated", "Publicação atualizada"
    DEFENSE_SCHEDULED = "defense.scheduled", "Banca agendada"
    DEFENSE_MEMBER_INVITED = "defense.member_invited", "Membro da banca convidado"
    DEFENSE_EVALUATION_RECORDED = "defense.evaluation_recorded", "Avaliação registrada"
    DEFENSE_COMPLETED = "defense.completed", "Banca concluída"
    DEFENSE_CANCELLED = "defense.cancelled", "Banca cancelada"
    DEFENSE_RESCHEDULED = "defense.rescheduled", "Banca remarcada"
    THESIS_REVIEW_RECORDED = "thesis.review_recorded", "Parecer do orientador registrado"
    THESIS_DEFENSE_RESULT_RECORDED = "thesis.defense_result_recorded", "Resultado da banca registrado"
    THESIS_PUBLISHED = "thesis.published", "Monografia publicada"
    DEFENSE_EXTERNAL_MEMBER_INVITED = "defense.external_member_invited", "Convite de membro externo emitido"


class ExternalInvitationTrigger(models.TextChoices):
    """
    Why an external member invitation e-mail (with a new token) was requested.
    """

    CREATED = "CREATED", "Convite criado"
    RESENT = "RESENT", "Convite reenviado"
    REGENERATED = "REGENERATED", "Novo convite gerado"


# Payloads levam apenas identificadores e códigos; nomes, e-mails, notas, local e
# link da banca são lidos do banco pelo consumidor. O conjunto de chaves é exato.
# O token do membro externo nunca entra no payload: o consumidor o gera e o envia
# só no e-mail, guardando apenas o hash no convite.
PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    EventType.THESIS_SUBMITTED: frozenset({"thesisId", "versionNumber", "submittedAt"}),
    EventType.THESIS_TEXT_EXTRACTION_REQUESTED: frozenset({"thesisId", "storedFileId", "trigger"}),
    EventType.THESIS_TEXT_EXTRACTED: frozenset({"thesisId", "storedFileId"}),
    EventType.THESIS_REPOSITORY_UPDATED: frozenset({"thesisId", "trigger"}),
    EventType.DEFENSE_SCHEDULED: frozenset({"defenseId", "thesisId", "scheduledDatetime", "durationMinutes", "modality"}),
    EventType.DEFENSE_MEMBER_INVITED: frozenset({"defenseId", "thesisId", "memberId", "invitationId"}),
    EventType.DEFENSE_EVALUATION_RECORDED: frozenset({"defenseId", "thesisId", "memberId", "isUpdate"}),
    EventType.DEFENSE_COMPLETED: frozenset({"defenseId", "thesisId", "result", "thesisNewStatus", "automatic"}),
    EventType.DEFENSE_CANCELLED: frozenset({"defenseId", "thesisId", "previousStatus"}),
    EventType.DEFENSE_RESCHEDULED: frozenset({"defenseId", "thesisId", "scheduledDatetime", "durationMinutes"}),
    EventType.THESIS_REVIEW_RECORDED: frozenset({"thesisId", "reviewDecision"}),
    EventType.THESIS_DEFENSE_RESULT_RECORDED: frozenset({"thesisId", "result", "thesisNewStatus"}),
    EventType.THESIS_PUBLISHED: frozenset({"thesisId", "referenceCode", "publishedAt"}),
    EventType.DEFENSE_EXTERNAL_MEMBER_INVITED: frozenset({"defenseId", "thesisId", "memberId", "invitationId", "trigger"}),
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
