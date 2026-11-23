"""Regression tests: test processes must not share mutable state.

Two agents running the suite at the same time must get isolated media, cache
and database names. Otherwise one run deletes the other run's uploads or
poisons its cache/throttle counters.

The assertions read settings in a fresh subprocess because Django's test
runner rewrites ``DATABASES[*]['NAME']`` to an in-memory database at runtime.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

_CODE = (
    "import json, os, django;"
    "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'thesismanagerbackend.settings_e2e');"
    "django.setup();"
    "from django.conf import settings;"
    "print(json.dumps({"
    "'pid': os.getpid(),"
    "'media': str(settings.MEDIA_ROOT),"
    "'cache': str(settings.CACHES['default']['LOCATION']),"
    "'db': str(settings.DATABASES['default']['NAME']),"
    "'cors': ','.join(settings.CORS_ALLOWED_ORIGINS),"
    "}))"
)


def _load_settings(run_id: str | None = None, frontend_port: str | None = None) -> dict[str, str | int]:
    environment = dict(os.environ)
    environment.pop("E2E_RUN_ID", None)
    environment.pop("E2E_DB_NAME", None)
    environment.pop("E2E_MEDIA_ROOT", None)
    environment.pop("E2E_FRONTEND_PORT", None)
    if run_id is not None:
        environment["E2E_RUN_ID"] = run_id
    if frontend_port is not None:
        environment["E2E_FRONTEND_PORT"] = frontend_port
    result = subprocess.run(
        [sys.executable, "-c", _CODE],
        cwd=Path(settings.BASE_DIR),
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


class ProcessIsolationTests(SimpleTestCase):
    """Every mutable path/key used by the test environment is process scoped."""

    def test_defaults_are_process_scoped(self) -> None:
        values = _load_settings()
        self.assertIn(str(values["pid"]), str(values["media"]))
        self.assertIn(str(values["pid"]), str(values["cache"]))
        self.assertIn(str(values["pid"]), str(values["db"]))

    def test_environment_override_is_shared_by_all_processes(self) -> None:
        values = _load_settings(run_id="isolation-token")
        self.assertIn("isolation-token", str(values["media"]))
        self.assertIn("isolation-token", str(values["cache"]))
        self.assertIn("isolation-token", str(values["db"]))

    def test_e2e_paths_can_be_overridden_by_environment(self) -> None:
        config = (Path(settings.BASE_DIR) / "thesismanagerbackend" / "settings_e2e.py").read_text()
        self.assertIn("E2E_DB_NAME", config)
        self.assertIn("E2E_MEDIA_ROOT", config)

    def test_cors_origin_follows_the_frontend_port(self) -> None:
        values = _load_settings(run_id="cors-token", frontend_port="45678")
        self.assertIn("45678", str(values["cors"]))

    def test_playwright_passes_frontend_port_to_backend(self) -> None:
        config = (Path(settings.BASE_DIR).parent / "thesis-manager-ufvjm-frontend" / "playwright.config.ts").read_text()
        self.assertIn("E2E_FRONTEND_PORT: String(frontendPort)", config)
