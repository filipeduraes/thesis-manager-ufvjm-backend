"""
Resolution of notification recipients from thesis and defense records.
"""

from __future__ import annotations

from dataclasses import dataclass

from accounts.models import ExternalInvitation, User
from theses.models import CommitteeRole, Defense, Thesis, ThesisAuthor


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
    author_links = ThesisAuthor.objects.filter(thesis=thesis).select_related("author").order_by("position")
    recipients: list[Recipient] = []
    for link in author_links:
        recipients.extend(_user_recipient(link.author))
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
    members = defense.members.exclude(role=CommitteeRole.ALTERNATE).select_related("institutional_member", "external_invitation").order_by("created_at")
    recipients: list[Recipient] = []
    for member in members:
        if member.institutional_member_id is not None:
            recipients.extend(_user_recipient(member.institutional_member))
        elif member.email:
            recipients.append(Recipient(name=member.name, external_invitation=member.external_invitation, external_email=member.email))
    return recipients


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
