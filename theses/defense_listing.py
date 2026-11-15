"""
Read-only listing services for defense sessions and schedulable theses.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import User
from theses.defense_scheduling import defense_payload, sync_defense_status
from theses.models import ACTIVE_DEFENSE_STATUSES, SCHEDULABLE_STATUSES, CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis
from theses.services import user_brief, user_is_admin, user_is_advisor, user_is_coordinator

logger = logging.getLogger(__name__)

DEFENSE_LIST_PREFETCH: tuple[str, ...] = ("members__institutional_member", "members__external_invitation", "members__evaluation", "thesis__authors__author")


def defenses_visible_to(principal: User | ExternalInvitationPrincipal) -> QuerySet[Defense]:
    """
    Return the defenses the given principal may read.

    - Admin / coordinator: every defense.
    - Advisor: defenses of advised theses plus committees they sit on.
    - Student: defenses of the thesis they mainly author plus committees they sit on.
    - ExternalInvitationPrincipal: only the committees bound to their invitation.

    Args:
        principal: Authenticated institutional user or external invitation principal.

    Returns:
        QuerySet[Defense]: Scoped queryset with thesis and members preloaded.
    """
    base: QuerySet[Defense] = Defense.objects.select_related("thesis", "thesis__advisor").prefetch_related(*DEFENSE_LIST_PREFETCH)
    if isinstance(principal, ExternalInvitationPrincipal):
        return base.filter(members__external_invitation=principal.invitation).distinct()
    if user_is_admin(principal) or user_is_coordinator(principal):
        return base
    membership = Q(members__institutional_member=principal)
    if user_is_advisor(principal):
        return base.filter(Q(thesis__advisor_id=principal.pk) | membership).distinct()
    main_authorship = Q(thesis__authors__author=principal, thesis__authors__is_main_author=True)
    return base.filter(main_authorship | membership).distinct()


def parse_defense_status_filter(raw_value: str | None) -> list[str]:
    """
    Parse a comma-separated ``status`` query parameter into valid DefenseStatus codes.

    Unknown codes are ignored so that a typo does not turn into a server error;
    an empty result means "no filter".

    Args:
        raw_value: Raw query parameter value (e.g. ``"SCHEDULED,IN_PROGRESS"``).

    Returns:
        list[str]: Valid, upper-cased DefenseStatus codes.
    """
    if not raw_value:
        return []
    candidates: list[str] = [value.strip().upper() for value in raw_value.split(",") if value.strip()]
    return [value for value in candidates if value in DefenseStatus.values]


def sync_due_defenses(queryset: QuerySet[Defense], request: HttpRequest | None = None) -> int:
    """
    Start every visible defense whose scheduled time has already passed.

    Runs before the status filter so that listings never show a stale
    ``SCHEDULED`` status that the detail endpoint would already report as
    ``IN_PROGRESS``.

    Args:
        queryset: Scoped defense queryset.
        request: HTTP request used for audit context.

    Returns:
        int: Number of defenses transitioned to IN_PROGRESS.
    """
    due_defenses = queryset.filter(status=DefenseStatus.SCHEDULED, auto_start_suspended=False, scheduled_datetime__lte=timezone.now())
    started_count = 0
    for defense in due_defenses:
        synced_defense = sync_defense_status(defense, request=request)
        if synced_defense.status == DefenseStatus.IN_PROGRESS:
            started_count += 1
    if started_count:
        logger.info("Auto-started %s overdue defenses while listing", started_count)
    return started_count


def list_visible_defenses(principal: User, status_filter: str | None = None, request: HttpRequest | None = None) -> QuerySet[Defense]:
    """
    Return the defenses visible to an institutional user, optionally filtered by status.

    Side effect: overdue scheduled defenses are auto-started (see ``sync_due_defenses``).

    Args:
        principal: Authenticated institutional user.
        status_filter: Optional comma-separated DefenseStatus codes.
        request: HTTP request used for audit context.

    Returns:
        QuerySet[Defense]: Scoped queryset ordered by most recent schedule first.
    """
    queryset = defenses_visible_to(principal)
    sync_due_defenses(queryset, request=request)
    statuses = parse_defense_status_filter(status_filter)
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    return queryset.order_by("-scheduled_datetime", "-created_at")


def schedulable_theses_for(principal: User) -> QuerySet[Thesis]:
    """
    Return theses for which the principal may schedule a defense right now.

    Mirrors ``schedule_defense``: status in SCHEDULABLE_STATUSES, no active defense,
    and the ``_can_manage_defense`` rule (admin/coordinator see all; advisors see
    only their own advised theses; anyone else sees nothing).

    Args:
        principal: Authenticated institutional user.

    Returns:
        QuerySet[Thesis]: Schedulable theses ordered by advisor approval date.
    """
    queryset = Thesis.objects.filter(deleted_at__isnull=True, status__in=SCHEDULABLE_STATUSES)
    queryset = queryset.exclude(defenses__status__in=ACTIVE_DEFENSE_STATUSES)
    if not (user_is_admin(principal) or user_is_coordinator(principal)):
        if not user_is_advisor(principal):
            return queryset.none()
        queryset = queryset.filter(advisor_id=principal.pk)
    queryset = queryset.select_related("course", "advisor")
    return queryset.distinct().order_by("advisor_approved_at", "updated_at")


def _viewer_membership(defense: Defense, viewer: User) -> DefenseMember | None:
    """
    Return the viewer's committee membership using the prefetched members.

    Args:
        defense: Defense with ``members`` prefetched.
        viewer: Authenticated institutional user.

    Returns:
        DefenseMember | None: Matching member, if any.
    """
    for member in defense.members.all():
        if member.institutional_member_id == viewer.pk:
            return member
    return None


def _member_has_evaluated(member: DefenseMember) -> bool:
    """
    Return whether a committee member already submitted an evaluation.

    Args:
        member: Committee member (``evaluation`` ideally prefetched).

    Returns:
        bool: True when an evaluation exists.
    """
    try:
        member.evaluation
    except ObjectDoesNotExist:
        return False
    return True


def _thesis_main_author(thesis: Thesis) -> User | None:
    """
    Return the main author using the prefetched ``authors`` relation.

    Args:
        thesis: Thesis with ``authors__author`` prefetched.

    Returns:
        User | None: Main author, if any.
    """
    for author_link in thesis.authors.all():
        if author_link.is_main_author:
            return author_link.author
    return None


def defense_list_payload(defense: Defense, viewer: User) -> dict[str, Any]:
    """
    Serialize a defense for the committee listing screen.

    Extends ``defense_payload`` with thesis summary fields and the viewer's own
    membership state, so the frontend can render the list without extra requests.

    Args:
        defense: Defense loaded through ``defenses_visible_to``.
        viewer: Authenticated institutional user.

    Returns:
        dict[str, Any]: Defense payload plus ``thesisTitle``, ``thesisReferenceCode``,
        ``student``, ``advisor`` and ``viewerMembership``.
    """
    payload = defense_payload(defense, viewer=viewer)
    thesis = defense.thesis
    main_author = _thesis_main_author(thesis)
    member = _viewer_membership(defense, viewer)
    payload["thesisTitle"] = thesis.title
    payload["thesisReferenceCode"] = thesis.reference_code
    payload["student"] = user_brief(main_author)
    payload["advisor"] = user_brief(thesis.advisor)
    payload["viewerMembership"] = {
        "isMember": member is not None,
        "memberId": str(member.pk) if member else None,
        "role": member.role if member else None,
        "roleLabel": CommitteeRole(member.role).label if member else None,
        "canEvaluate": member is not None and member.role != CommitteeRole.ALTERNATE,
        "hasEvaluated": _member_has_evaluated(member) if member else False,
    }
    return payload
