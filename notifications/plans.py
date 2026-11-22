"""
Types shared by the event planners of the notification consumer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from notifications.models import Notification
from notifications.recipients import Recipient
from notifications.rendering import TemplateContext
from theses.models import Thesis


class EntityNotFoundError(LookupError):
    """
    Raised when the thesis or defense referenced by an event no longer exists.
    """


@dataclass
class NotificationPlan:
    """
    What to send for one event.

    Attributes:
        template_code: Template used for every recipient.
        thesis: Thesis the event refers to.
        recipients: Deduplicated recipients.
        context: Template variables shared by every recipient (stored in ``notification``).
        secret_context: Variables that override ``context`` only in the message actually
            sent; they are kept in memory and never stored, logged or audited.
        after_send: Called with each notification accepted by the e-mail backend.
    """

    template_code: str
    thesis: Thesis
    recipients: list[Recipient]
    context: TemplateContext = field(default_factory=dict)
    secret_context: TemplateContext = field(default_factory=dict)
    after_send: Callable[[Notification], None] | None = None
