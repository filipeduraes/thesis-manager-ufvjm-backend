#!/usr/bin/env python3
"""Django's command-line utility for administrative tasks."""
import os
import sys

TEST_COMMANDS = frozenset({"test", "test_fast"})


def default_settings_module(argv: list[str]) -> str:
    """Return the settings module matching a management command.

    Test commands must use the process-scoped E2E settings so that two agents
    running the suite at the same time do not share media, cache or database
    state. An explicit ``DJANGO_SETTINGS_MODULE`` in the environment still wins.
    """
    if len(argv) > 1 and argv[1] in TEST_COMMANDS:
        return "thesismanagerbackend.settings_e2e"
    return "thesismanagerbackend.settings"


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", default_settings_module(sys.argv))
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
