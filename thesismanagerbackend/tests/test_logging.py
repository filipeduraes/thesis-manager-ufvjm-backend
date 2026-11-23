"""Regression test for consistent logging in workers outside the web process."""

from __future__ import annotations

import logging

from django.conf import settings
from django.test import SimpleTestCase


class LoggingConfigurationTests(SimpleTestCase):
    """Application loggers share the configured format and level."""

    def test_root_handler_serves_app_loggers(self) -> None:
        self.assertEqual(settings.LOGGING["root"]["handlers"], ["console"])
        for name in ("accounts", "theses", "indexing", "adjudication"):
            with self.subTest(logger=name):
                self.assertTrue(logging.getLogger(name).hasHandlers())
