"""
Test helpers that prove an external invitation token exists only in the e-mail.
"""

from __future__ import annotations

import io
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

from django.core import mail
from django.core.management import call_command
from django.test import TestCase

from accounts.models import AuditLog
from events.envelope import EventEnvelope
from events.models import OutboxEvent
from notifications.models import Notification

TOKEN_PATTERN = re.compile(r"UFVJM(?:-[A-Z0-9]{4}){4}")


def tokens_in_outbox() -> list[str]:
    """
    Return every readable token found in the messages sent so far (text and HTML).

    Returns:
        list[str]: Tokens in sending order (one per message part where it appears).
    """
    found: list[str] = []
    for message in mail.outbox:
        parts = [message.subject, message.body] + [content for content, _mimetype in message.alternatives]
        for part in parts:
            found.extend(TOKEN_PATTERN.findall(part))
    return found


def last_emailed_token(test_case: TestCase) -> str:
    """
    Return the token of the last e-mail, checking text and HTML carry the same one.

    Args:
        test_case: Test case used for assertions.

    Returns:
        str: Readable token.
    """
    message = mail.outbox[-1]
    text_tokens = TOKEN_PATTERN.findall(message.body)
    html_tokens = TOKEN_PATTERN.findall(message.alternatives[0][0])
    test_case.assertEqual(len(text_tokens), 1)
    test_case.assertEqual(text_tokens, html_tokens)
    return text_tokens[0]


@contextmanager
def capture_all_logs(test_case: TestCase) -> Iterator[object]:
    """
    Capture records of every logger at DEBUG level with ``assertLogs``.

    ``events`` and ``notifications`` do not propagate to the root logger in the
    project settings, so propagation is forced on (and levels reset) for every
    known logger while the block runs, then restored.

    Args:
        test_case: Test case providing ``assertLogs``.

    Yields:
        object: The ``assertLogs`` watcher (``records`` and ``output``).
    """
    known_loggers = [logger for logger in logging.root.manager.loggerDict.values() if isinstance(logger, logging.Logger)]
    saved_state = [(logger, logger.propagate, logger.level, logger.disabled) for logger in known_loggers]
    for logger in known_loggers:
        logger.propagate = True
        logger.setLevel(logging.NOTSET)
        logger.disabled = False
    try:
        with test_case.assertLogs(level="DEBUG") as captured:
            yield captured
    finally:
        for logger, propagate, level, disabled in saved_state:
            logger.propagate = propagate
            logger.setLevel(level)
            logger.disabled = disabled


def assert_token_not_logged(test_case: TestCase, captured: object, token: str) -> None:
    """
    Assert that no captured log message, argument or traceback contains the token.

    Args:
        test_case: Test case used for assertions.
        captured: Watcher returned by ``capture_all_logs``.
        token: Readable token.
    """
    formatter = logging.Formatter()
    for record in captured.records:
        rendered = record.getMessage()
        if record.exc_info:
            rendered += formatter.formatException(record.exc_info)
        test_case.assertNotIn(token, rendered)
        test_case.assertIsNone(TOKEN_PATTERN.search(rendered), f"token-like value logged by {record.name}")
    test_case.assertNotIn(token, "\n".join(captured.output))


def assert_token_not_stored(test_case: TestCase, token: str) -> None:
    """
    Assert that the token is absent from the outbox, notifications, audit log and every other table.

    Args:
        test_case: Test case used for assertions.
        token: Readable token.
    """
    for event in OutboxEvent.objects.all():
        envelope_bytes = EventEnvelope.from_outbox(event).to_json_bytes()
        test_case.assertNotIn(token.encode(), envelope_bytes)
        stored_event = json.dumps({"payload": event.payload, "headers": event.headers, "last_error": event.last_error}, default=str)
        test_case.assertNotIn(token, stored_event)
    for notification in Notification.objects.all():
        stored_notification = " ".join([notification.subject, notification.body, notification.body_html, notification.error_message])
        test_case.assertNotIn(token, stored_notification)
        test_case.assertIsNone(TOKEN_PATTERN.search(stored_notification))
    for entry in AuditLog.objects.all():
        stored_audit = json.dumps({"changes": entry.changes, "metadata": entry.metadata, "user_agent": entry.user_agent}, default=str)
        test_case.assertNotIn(token, stored_audit)
    database_dump = io.StringIO()
    call_command("dumpdata", stdout=database_dump)
    test_case.assertNotIn(token, database_dump.getvalue())


def assert_envelopes_without_token(test_case: TestCase, envelopes: list[EventEnvelope], token: str) -> None:
    """
    Assert that no published envelope carries the token.

    Args:
        test_case: Test case used for assertions.
        envelopes: Envelopes published to the (fake) broker.
        token: Readable token.
    """
    for envelope in envelopes:
        test_case.assertNotIn(token.encode(), envelope.to_json_bytes())
