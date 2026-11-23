"""
Shared fixtures helpers for defense tests.
"""

from __future__ import annotations

from django.utils import timezone

from theses.models import AvailabilityStatus, Defense, DefenseMember, DefenseMemberEvaluation


def confirm_committee(defense: Defense) -> int:
    """
    Mark every committee member of the defense as having confirmed availability.

    Used by fixtures that start a defense, now that starting requires every
    non-alternate member to be CONFIRMED.

    Args:
        defense: Defense whose members are confirmed.

    Returns:
        int: Number of members updated.
    """
    return DefenseMember.objects.filter(defense=defense).update(availability_status=AvailabilityStatus.CONFIRMED, availability_responded_at=timezone.now())


def record_evaluation_without_auto_close(member: DefenseMember, decision: str) -> DefenseMemberEvaluation:
    """
    Store an evaluation directly, bypassing the automatic close of the service.

    Used by fixtures that exercise the manual ``close_defense`` path: going through
    ``submit_committee_evaluation`` for the last evaluation would close the defense
    automatically before the test reaches the manual close.

    Args:
        member: Committee member being evaluated.
        decision: DefenseResult code.

    Returns:
        DefenseMemberEvaluation: Created evaluation.
    """
    return DefenseMemberEvaluation.objects.create(member=member, decision=decision, score="8.00", notes="Parecer de teste.")
