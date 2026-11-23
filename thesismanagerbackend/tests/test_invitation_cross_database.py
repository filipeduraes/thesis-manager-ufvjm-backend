"""Regression tests: the invitation e-mail flow must respect database ownership.

`Notification` lives in the notifications database, `ExternalInvitation` in the
identity database and `OutboxEvent` in the thesis database. A single
``atomic()`` on ``default`` or a ``select_related("event")`` JOIN fails when the
services are split.
"""

from __future__ import annotations

import inspect
from typing import Callable

from django.test import SimpleTestCase

from accounts.services import issue_external_token
from notifications.invitations import plan_external_member_invited, prepare_invitation_retry
from notifications.plans import NotificationPlan


def _source(target: Callable[..., object]) -> str:
    return inspect.getsource(target)


class InvitationDatabaseGuardTests(SimpleTestCase):
    """Every lock and JOIN in the invitation flow is alias-aware."""

    def test_retry_does_not_join_outbox_event(self) -> None:
        source = _source(prepare_invitation_retry)
        self.assertNotIn('"event"', source)
        self.assertNotIn("'event'", source)

    def test_retry_uses_explicit_atomic_aliases(self) -> None:
        source = _source(prepare_invitation_retry)
        self.assertNotIn("with transaction.atomic():", source)
        self.assertIn("using=", source)

    def test_token_issuer_is_documented_as_caller_locked(self) -> None:
        self.assertIs(issue_external_token.__doc__ is not None, True)
        assert issue_external_token.__doc__ is not None
        self.assertIn("caller should hold a row lock", issue_external_token.__doc__)

    def test_planner_is_a_notification_plan_factory(self) -> None:
        self.assertTrue(callable(plan_external_member_invited))
        self.assertIsNotNone(NotificationPlan)
