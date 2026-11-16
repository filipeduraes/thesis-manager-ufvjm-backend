"""
Read-only listing services for defense sessions and schedulable theses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.http import HttpRequest
from django.utils import timezone

from accounts.authentication import ExternalInvitationPrincipal
from accounts.models import User
from accounts.services import google_social_accounts_prefetch
from theses.defense_committee import DefenseViewerContext, build_defense_viewer_context, coordinator_managed_theses
from theses.defense_scheduling import defense_close_entries_prefetch, defense_payload, sync_defense_status
from theses.models import ACTIVE_DEFENSE_STATUSES, SCHEDULABLE_STATUSES, CommitteeRole, Defense, DefenseMember, DefenseStatus, Thesis
from theses.services import user_brief, user_is_admin, user_is_advisor, user_is_coordinator

logger = logging.getLogger(__name__)

DEFENSE_LIST_PREFETCH: tuple[str, ...] = ("members__institutional_member", "members__external_invitation", "members__evaluation", "thesis__authors__author", "thesis__keyword_links__keyword", "thesis__files__stored_file")
TRUTHY_QUERY_VALUES: frozenset[str] = frozenset({"1", "true", "yes"})


@dataclass(frozen=True)
class DefenseListFilters:
    """
    Search and filter options accepted by the defense listing.

    Attributes:
        query: Free-text search (``q``), already stripped; empty means no search.
        status: Raw comma-separated DefenseStatus codes (``status``).
        pending_evaluation: Whether to keep only defenses awaiting the viewer's evaluation.
    """

    query: str = ""
    status: str = ""
    pending_evaluation: bool = False

    @classmethod
    def from_query_params(cls, params: Any) -> DefenseListFilters:
        """
        Build filters from request query parameters, following ``GET /api/v1/theses`` conventions.

        Args:
            params: Query parameter mapping (``request.query_params``).

        Returns:
            DefenseListFilters: Parsed filters.
        """
        query = (params.get("q") or "").strip()
        status = (params.get("status") or "").strip()
        pending_value = (params.get("pendingEvaluation") or "").strip().lower()
        return cls(query=query, status=status, pending_evaluation=pending_value in TRUTHY_QUERY_VALUES)


def defenses_visible_to(principal: User | ExternalInvitationPrincipal) -> QuerySet[Defense]:
    """
    Return the defenses the given principal may read.

    - Admin: every defense.
    - Coordinator: defenses of theses from the courses they coordinate.
    - Advisor: defenses of advised theses.
    - Anyone else (student): the defense of the thesis they mainly author.
    - Every institutional user also sees the committees they sit on.
    - ExternalInvitationPrincipal: only the committees bound to their invitation.

    Args:
        principal: Authenticated institutional user or external invitation principal.

    Returns:
        QuerySet[Defense]: Scoped queryset with thesis and members preloaded.
    """
    base: QuerySet[Defense] = Defense.objects.select_related("thesis", "thesis__advisor", "thesis__course", "thesis__course__department").prefetch_related(*DEFENSE_LIST_PREFETCH, defense_close_entries_prefetch(), google_social_accounts_prefetch("members__institutional_member__socialaccount_set"))
    if isinstance(principal, ExternalInvitationPrincipal):
        return base.filter(members__external_invitation=principal.invitation).distinct()
    if user_is_admin(principal):
        return base
    is_coordinator = user_is_coordinator(principal)
    is_advisor = user_is_advisor(principal)
    visibility = Q(members__institutional_member=principal)
    if is_coordinator:
        managed_thesis_ids = coordinator_managed_theses(principal).values("pk")
        visibility |= Q(thesis_id__in=managed_thesis_ids)
    if is_advisor:
        visibility |= Q(thesis__advisor_id=principal.pk)
    if not (is_coordinator or is_advisor):
        visibility |= Q(thesis__authors__author=principal, thesis__authors__is_main_author=True)
    return base.filter(visibility).distinct()


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


def _search_defenses(queryset: QuerySet[Defense], query: str) -> QuerySet[Defense]:
    """
    Apply a case-insensitive free-text search to a defense queryset.

    Matches thesis title, thesis reference code, main author name, advisor name
    and defense location. The main-author conditions share one ``Q`` so they hit
    the same author row (a co-author's name does not match).

    Args:
        queryset: Scoped defense queryset.
        query: Non-empty search term.

    Returns:
        QuerySet[Defense]: Filtered queryset (may contain duplicates; caller applies distinct).
    """
    main_author_match = Q(thesis__authors__is_main_author=True, thesis__authors__author__name__icontains=query)
    return queryset.filter(
        Q(thesis__title__icontains=query)
        | Q(thesis__reference_code__icontains=query)
        | main_author_match
        | Q(thesis__advisor__name__icontains=query)
        | Q(location__icontains=query)
    )


def _pending_evaluation_for(queryset: QuerySet[Defense], principal: User) -> QuerySet[Defense]:
    """
    Keep only active defenses where the principal is a non-alternate member without an evaluation.

    Only SCHEDULED and IN_PROGRESS defenses are kept: a cancelled or completed
    defense can no longer receive evaluations, so it is not "pending". An
    ``Exists`` subquery guarantees that role and evaluation are checked on the
    same membership row.

    Args:
        queryset: Scoped defense queryset.
        principal: Authenticated institutional user.

    Returns:
        QuerySet[Defense]: Filtered queryset.
    """
    pending_memberships = DefenseMember.objects.filter(defense=OuterRef("pk"), institutional_member=principal, evaluation__isnull=True)
    pending_memberships = pending_memberships.exclude(role=CommitteeRole.ALTERNATE)
    return queryset.filter(Exists(pending_memberships), status__in=ACTIVE_DEFENSE_STATUSES)


def list_visible_defenses(principal: User, filters: DefenseListFilters | None = None, request: HttpRequest | None = None) -> QuerySet[Defense]:
    """
    Return the defenses visible to an institutional user, with optional search and filters.

    Side effect: overdue scheduled defenses are auto-started (see ``sync_due_defenses``)
    before any filter runs, so ``status`` and ``pendingEvaluation`` see current statuses.

    Args:
        principal: Authenticated institutional user.
        filters: Search/filter options; ``None`` means no filtering.
        request: HTTP request used for audit context.

    Returns:
        QuerySet[Defense]: Distinct scoped queryset ordered by most recent schedule first.
    """
    active_filters = filters or DefenseListFilters()
    queryset = defenses_visible_to(principal)
    sync_due_defenses(queryset, request=request)
    statuses = parse_defense_status_filter(active_filters.status)
    if statuses:
        queryset = queryset.filter(status__in=statuses)
    if active_filters.query:
        queryset = _search_defenses(queryset, active_filters.query)
    if active_filters.pending_evaluation:
        queryset = _pending_evaluation_for(queryset, principal)
    return queryset.distinct().order_by("-scheduled_datetime", "-created_at")


def schedulable_theses_for(principal: User) -> QuerySet[Thesis]:
    """
    Return theses for which the principal may schedule a defense right now.

    Mirrors ``schedule_defense``: status in SCHEDULABLE_STATUSES, no active defense,
    and the ``_can_manage_defense`` rule (admin sees all; coordinators see theses of
    the courses they coordinate; advisors see their own advised theses; anyone else
    sees nothing).

    Args:
        principal: Authenticated institutional user.

    Returns:
        QuerySet[Thesis]: Schedulable theses ordered by advisor approval date.
    """
    queryset = Thesis.objects.filter(deleted_at__isnull=True, status__in=SCHEDULABLE_STATUSES)
    queryset = queryset.exclude(defenses__status__in=ACTIVE_DEFENSE_STATUSES)
    if not user_is_admin(principal):
        allowed = Q(pk__in=[])
        if user_is_coordinator(principal):
            managed_thesis_ids = coordinator_managed_theses(principal).values("pk")
            allowed |= Q(pk__in=managed_thesis_ids)
        if user_is_advisor(principal):
            allowed |= Q(advisor_id=principal.pk)
        queryset = queryset.filter(allowed)
    queryset = queryset.select_related("course", "advisor").prefetch_related("authors__author", "keyword_links__keyword", "files__stored_file")
    return queryset.distinct().order_by("advisor_approved_at", "updated_at")


def _viewer_membership(defense: Defense, viewer: User | ExternalInvitationPrincipal) -> DefenseMember | None:
    """
    Return the viewer's committee membership using the prefetched members.

    Args:
        defense: Defense with ``members`` prefetched.
        viewer: Institutional user or external invitation principal.

    Returns:
        DefenseMember | None: Matching member, if any.
    """
    for member in defense.members.all():
        if isinstance(viewer, ExternalInvitationPrincipal):
            if member.external_invitation_id is not None and member.external_invitation_id == viewer.invitation.pk:
                return member
        elif member.institutional_member_id is not None and member.institutional_member_id == viewer.pk:
            return member
    return None


def viewer_membership_payload(defense: Defense, viewer: User | ExternalInvitationPrincipal) -> dict[str, Any]:
    """
    Serialize the viewer's own committee membership state for a defense.

    Shared by the listing and the detail so both return the same shape, for
    institutional users and external invitation sessions.

    Args:
        defense: Defense (``members`` and ``members__evaluation`` ideally prefetched).
        viewer: Institutional user or external invitation principal.

    Returns:
        dict[str, Any]: ``isMember``, ``memberId``, ``role``, ``roleLabel``, ``canEvaluate``, ``hasEvaluated``.
    """
    member = _viewer_membership(defense, viewer)
    return {
        "isMember": member is not None,
        "memberId": str(member.pk) if member else None,
        "role": member.role if member else None,
        "roleLabel": CommitteeRole(member.role).label if member else None,
        "canEvaluate": member is not None and member.role != CommitteeRole.ALTERNATE,
        "hasEvaluated": _member_has_evaluated(member) if member else False,
    }


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


def defense_viewer_context_for(defenses: list[Defense], viewer: User | ExternalInvitationPrincipal) -> DefenseViewerContext:
    """
    Resolve the viewer's management rights once for a page of defenses.

    Args:
        defenses: Defenses about to be serialized.
        viewer: Institutional user or external invitation principal.

    Returns:
        DefenseViewerContext: Context covering every thesis of ``defenses``.
    """
    thesis_ids = {defense.thesis_id for defense in defenses}
    return build_defense_viewer_context(viewer, thesis_ids)


def defense_list_payload(defense: Defense, viewer: User, viewer_context: DefenseViewerContext) -> dict[str, Any]:
    """
    Serialize a defense for the committee listing screen.

    Extends ``defense_payload`` with thesis summary fields, the viewer's own
    membership state and ``viewerPermissions``, so the frontend can render the
    list without extra requests. ``thesisSummary`` carries the same thesis data in
    a nested object; the flat ``thesisTitle``/``student``/``advisor`` fields are
    kept for compatibility with existing clients.

    Args:
        defense: Defense loaded through ``defenses_visible_to``.
        viewer: Authenticated institutional user.
        viewer_context: Viewer rights resolved once for the page (``defense_viewer_context_for``).

    Returns:
        dict[str, Any]: Defense payload plus ``thesisTitle``, ``thesisReferenceCode``,
        ``student``, ``advisor``, ``viewerMembership`` and ``viewerPermissions``.
    """
    payload = defense_payload(defense, viewer=viewer, viewer_context=viewer_context)
    thesis = defense.thesis
    main_author = _thesis_main_author(thesis)
    payload["thesisTitle"] = thesis.title
    payload["thesisReferenceCode"] = thesis.reference_code
    payload["student"] = user_brief(main_author)
    payload["advisor"] = user_brief(thesis.advisor)
    payload["viewerMembership"] = viewer_membership_payload(defense, viewer)
    viewer_permissions = viewer_context.permissions_for(defense)
    payload["viewerPermissions"] = viewer_permissions.to_payload()
    return payload
