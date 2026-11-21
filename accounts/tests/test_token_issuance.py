"""
Tests for the rotation helpers that issue external invitation tokens.
"""
from __future__ import annotations

import uuid

from django.test import RequestFactory, TestCase

from accounts.models import ExternalInvitation
from accounts.services import ExternalTokenError, create_external_invitation, exchange_external_token, hash_external_token, invalidate_external_token, issue_external_token, mark_external_token_sent, unissued_token_hash
from accounts.tests.test_auth_services import TestSession


class ExternalTokenIssuanceTests(TestCase):
    """
    Token rotation used by the e-mail delivery of external invitations.
    """

    def setUp(self) -> None:
        self.factory = RequestFactory()
        self.invitation, _token = create_external_invitation(name="Membro Externo", email="externo@outra.org", institution="UFMG")

    def _exchange(self, token: str) -> ExternalInvitation:
        request = self.factory.post("/api/v1/auth/external-token")
        request.session = TestSession()
        return exchange_external_token(token, request)

    def test_issue_stores_only_the_hash_of_the_new_token(self) -> None:
        token = issue_external_token(self.invitation)
        self.invitation.refresh_from_db()
        self.assertTrue(token.startswith("UFVJM-"))
        self.assertEqual(bytes(self.invitation.token_hash), hash_external_token(token))
        self.assertNotIn(token.encode(), bytes(self.invitation.token_hash))

    def test_each_issue_changes_the_hash_and_invalidates_the_previous_token(self) -> None:
        first_token = issue_external_token(self.invitation)
        first_hash = bytes(self.invitation.token_hash)
        second_token = issue_external_token(self.invitation)
        self.assertNotEqual(first_token, second_token)
        self.assertNotEqual(first_hash, bytes(self.invitation.token_hash))
        with self.assertRaises(ExternalTokenError):
            self._exchange(first_token)
        self.assertEqual(self._exchange(second_token).pk, self.invitation.pk)

    def test_invalidate_makes_the_current_token_stop_working(self) -> None:
        token = issue_external_token(self.invitation)
        self.invitation.token_sent_at = self.invitation.created_at
        self.invitation.save(update_fields=["token_sent_at"])
        invalidate_external_token(self.invitation)
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.token_sent_at)
        with self.assertRaises(ExternalTokenError):
            self._exchange(token)

    def test_unissued_hash_is_random_and_matches_no_token(self) -> None:
        first = unissued_token_hash()
        second = unissued_token_hash()
        self.assertEqual(len(first), 32)
        self.assertNotEqual(first, second)
        invalidate_external_token(self.invitation)
        self.assertFalse(ExternalInvitation.objects.filter(token_hash=hash_external_token("UFVJM-")).exists())

    def test_mark_sent_only_for_the_current_issuing_event(self) -> None:
        current_event = uuid.uuid4()
        self.invitation.token_event_id = current_event
        self.invitation.save(update_fields=["token_event_id"])
        self.assertFalse(mark_external_token_sent(self.invitation.pk, uuid.uuid4()))
        self.invitation.refresh_from_db()
        self.assertIsNone(self.invitation.token_sent_at)
        self.assertTrue(mark_external_token_sent(self.invitation.pk, current_event))
        self.invitation.refresh_from_db()
        self.assertIsNotNone(self.invitation.token_sent_at)
