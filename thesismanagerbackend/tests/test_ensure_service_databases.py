"""Tests of the service database bootstrap command."""

from __future__ import annotations

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

COMMAND = "accounts.management.commands.ensure_service_databases"


@override_settings(
    DATABASES={
        "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "thesis_manager", "USER": "u", "PASSWORD": "p", "HOST": "db", "PORT": "5432"},
        "auth": {"ENGINE": "django.db.backends.postgresql", "NAME": "thesis_auth", "USER": "u", "PASSWORD": "p", "HOST": "db", "PORT": "5432"},
    },
    SERVICE_DATABASE_ALIASES=("auth",),
)
class EnsureServiceDatabasesTests(SimpleTestCase):
    """Missing databases are created, existing ones are left alone."""

    def test_creates_only_missing_databases(self) -> None:
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor
        cursor.fetchone.side_effect = [None]
        connection = MagicMock()
        connection.__enter__.return_value.cursor.return_value = cursor
        with patch("psycopg.connect", return_value=connection) as connect:
            output = StringIO()
            call_command("ensure_service_databases", stdout=output)
        connect.assert_called_once()
        created = [call.args[0] for call in cursor.execute.call_args_list if "CREATE DATABASE" in call.args[0]]
        self.assertEqual(created, ['CREATE DATABASE "thesis_auth"'])
        self.assertIn("Banco thesis_auth criado.", output.getvalue())
