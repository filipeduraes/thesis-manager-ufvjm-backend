"""
Create the per-service databases when they do not exist.

The PostgreSQL image only runs ``/docker-entrypoint-initdb.d`` scripts on an
empty data directory, so an existing volume does not get the new databases.
This command connects to the maintenance database and creates any missing one
before migrations run.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Ensure every service database alias has a physical PostgreSQL database."""

    help = "Cria os bancos de dados dos serviços que ainda não existem."

    def handle(self, *args, **options) -> None:
        """Create missing databases using the default connection parameters."""
        import psycopg

        source = settings.DATABASES["default"]
        params: dict[str, object] = {
            "dbname": "postgres",
            "user": source.get("USER", ""),
            "password": source.get("PASSWORD", ""),
            "host": source.get("HOST", ""),
            "port": source.get("PORT", ""),
        }
        with psycopg.connect(**params, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for alias in getattr(settings, "SERVICE_DATABASE_ALIASES", ()):
                    name = settings.DATABASES[alias]["NAME"]
                    cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
                    if cursor.fetchone():
                        self.stdout.write(f"Banco {name} já existe.")
                        continue
                    cursor.execute(f'CREATE DATABASE "{name}"')
                    self.stdout.write(f"Banco {name} criado.")
