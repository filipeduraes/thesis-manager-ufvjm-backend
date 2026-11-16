"""
Shared fixtures helpers for defense tests.
"""

from __future__ import annotations

from django.utils import timezone

from theses.models import AvailabilityStatus, Defense, DefenseMember


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
