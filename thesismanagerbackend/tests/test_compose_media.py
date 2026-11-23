"""Regression test: uploaded files live in a removable named volume."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class ComposeMediaVolumeTests(SimpleTestCase):
    """The Compose file keeps uploads out of the bind-mounted source tree."""

    def test_media_uses_named_volume(self) -> None:
        content = (Path(settings.BASE_DIR) / "docker-compose.yml").read_text()
        self.assertIn("media_data:/app/media", content)
        self.assertIn("  media_data:", content)
        self.assertIn("MEDIA_ROOT: /app/media", content)
        self.assertNotIn("      - ./media:/app/media", content)
