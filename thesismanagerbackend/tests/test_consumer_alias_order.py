"""Regression tests: consumers commit service rows before the idempotency marker.

With split databases the marker lives in ``default`` while notifications,
repository projections and reporting facts live in their own databases. A
single ``atomic()`` cannot span them; if the marker is written first, a later
failure permanently drops the event because the consumer will skip the replay.
"""

from __future__ import annotations

import inspect
import textwrap
from typing import Callable

from django.test import SimpleTestCase

from notifications.consumer import handle_event as handle_notification_event
from repository_readmodel.projections import handle_event as handle_repository_event
from reports_readmodel.projections import handle_event as handle_report_event


def _source(target: Callable[..., object]) -> str:
    return textwrap.dedent(inspect.getsource(target))


def _assert_rows_before_marker(target: Callable[..., object], write_token: str) -> None:
    source = _source(target)
    assert "with transaction.atomic():" not in source
    assert "using=" in source
    assert source.index(write_token) < source.index("ProcessedEvent.objects.using(marker_db).create(")


class ConsumerAliasOrderTests(SimpleTestCase):
    """Rows are durable before the replay guard is written."""

    def test_notifications_rows_precede_marker(self) -> None:
        _assert_rows_before_marker(handle_notification_event, "_create_notifications(")

    def test_repository_rows_precede_marker(self) -> None:
        _assert_rows_before_marker(handle_repository_event, "project_thesis(")

    def test_reporting_rows_precede_marker(self) -> None:
        _assert_rows_before_marker(handle_report_event, "fact.save()")
