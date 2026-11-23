"""Run the curated fast regression suite on every change."""

from __future__ import annotations

from django.core.management import call_command
from django.core.management.base import BaseCommand

from thesismanagerbackend.test_suites import FAST_TEST_LABELS


class Command(BaseCommand):
    """Execute ``FAST_TEST_LABELS`` with the standard Django test runner."""

    help = "Runs the fast regression suite (domain rules, routing, permissions and API contracts)."

    def handle(self, *args, **options) -> None:
        self.stdout.write(f"Running {len(FAST_TEST_LABELS)} fast test modules")
        call_command("test", *FAST_TEST_LABELS, verbosity=options["verbosity"], interactive=False)
