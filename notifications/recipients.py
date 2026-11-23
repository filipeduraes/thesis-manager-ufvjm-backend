"""
Resolution of notification recipients from thesis and defense records.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import ExternalInvitation, User
from theses.models import CommitteeRole, Defense, DefenseMember, Thesis, ThesisAuthor


@dataclass(frozen=True)
class Recipient:
    """
    Person who receives one notification.

    Attributes:
        name: Name used in the greeting.
        user: Institutional user, when the recipient has an account.
        external_invitation: Invitation of an external committee member.
        external_email: Address of an external member (empty for institutional users).
    """

    name: str
    user: User | None = None
    external_invitation: ExternalInvitation | None = None
    external_email: str = ""

    @property
    def dedup_key(self) -> str:
        """
        Identity used to avoid sending the same message twice to one person.

        Returns:
            str: User id, or the lower-cased external address.
        """
        if self.user is not None:
            return f"user:{self.user.pk}"
        return f"email:{self.external_email.lower()}"


def _user_recipient(user: User | None) -> list[Recipient]:
    """
    Wrap an active user as a recipient.

    Args:
        user: User or None.

    Returns:
        list[Recipient]: One recipient, or none for a missing or inactive user.
    """
    if user is None or not user.is_active:
        return []
    return [Recipient(name=user.name, user=user)]


def thesis_authors(thesis: Thesis) -> list[Recipient]:
    """
    Return the active authors of a thesis in author order.

    Args:
        thesis: Thesis instance.

    Returns:
        list[Recipient]: Author recipients.
    """
    from accounts.models import User

    author_links = list(ThesisAuthor.objects.filter(thesis=thesis).order_by("position"))
    user_ids = [link.author_id for link in author_links if link.author_id]
    users = {user.pk: user for user in User.objects.filter(pk__in=user_ids, is_active=True)}
    recipients: list[Recipient] = []
    for link in author_links:
        user = users.get(link.author_id)
        if user is not None:
            recipients.append(Recipient(name=user.name or link.name, user=user))
    return recipients


def thesis_advisor(thesis: Thesis) -> list[Recipient]:
    """
    Return the thesis advisor when active.

    Args:
        thesis: Thesis instance (with ``advisor`` loaded or loadable).

    Returns:
        list[Recipient]: Zero or one recipient.
    """
    return _user_recipient(thesis.advisor)


def evaluating_committee_members(defense: Defense) -> list[Recipient]:
    """
    Return the non-alternate committee members, institutional or external.

    Args:
        defense: Defense instance.

    Returns:
        list[Recipient]: Committee recipients.
    """
    from accounts.models import ExternalInvitation, User

    members = list(defense.members.exclude(role=CommitteeRole.ALTERNATE).order_by("created_at"))
    user_ids = [m.institutional_member_id for m in members if m.institutional_member_id]
    users = {u.pk: u for u in User.objects.filter(pk__in=user_ids, is_active=True)}
    invitation_ids = [m.external_invitation_id for m in members if m.external_invitation_id]
    invitations = {i.pk: i for i in ExternalInvitation.objects.filter(pk__in=invitation_ids)}
    recipients: list[Recipient] = []
    for member in members:
        if member.institutional_member_id is not None:
            user = users.get(member.institutional_member_id)
            if user is not None:
                recipients.append(Recipient(name=user.name, user=user))
        elif member.email:
            recipients.append(Recipient(name=member.name, external_invitation=invitations.get(member.external_invitation_id), external_email=member.email))
    return recipients


def defense_member(member: DefenseMember) -> list[Recipient]:
    """
    Return the single recipient of one committee membership.

    Args:
        member: Institutional or external committee member.

    Returns:
        list[Recipient]: One recipient, or none for an inactive institutional user.
    """
    if member.institutional_member_id is not None:
        return _user_recipient(member.institutional_member)
    if member.email:
        return [Recipient(name=member.name, external_invitation=member.external_invitation, external_email=member.email)]
    return []


def unique_recipients(*groups: list[Recipient]) -> list[Recipient]:
    """
    Merge recipient groups keeping the first occurrence of each person.

    The advisor is often also the committee president, and must get one message.

    Args:
        *groups: Recipient lists in priority order.

    Returns:
        list[Recipient]: Deduplicated recipients.
    """
    seen: set[str] = set()
    merged: list[Recipient] = []
    for group in groups:
        for recipient in group:
            if recipient.dedup_key in seen:
                continue
            seen.add(recipient.dedup_key)
            merged.append(recipient)
    return merged
