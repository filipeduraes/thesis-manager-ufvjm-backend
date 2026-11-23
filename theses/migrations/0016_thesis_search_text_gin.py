"""
GIN index for PostgreSQL full-text search in the public repository.

SQLite (used by the isolated E2E suite) does not support this index, so it is
created only on PostgreSQL. The application falls back to accent-insensitive
``icontains`` matching on other backends.
"""
from django.db import migrations

INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS thesis_search_text_gin "
    "ON thesis USING gin (to_tsvector('portuguese', search_text))"
)
DROP_SQL = "DROP INDEX IF EXISTS thesis_search_text_gin"


def create_index(apps, schema_editor):
    """
    Create the GIN expression index on PostgreSQL only.

    Args:
        apps: Migration app registry.
        schema_editor: Active database schema editor.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(INDEX_SQL)


def drop_index(apps, schema_editor):
    """
    Drop the GIN expression index on PostgreSQL only.

    Args:
        apps: Migration app registry.
        schema_editor: Active database schema editor.
    """
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(DROP_SQL)


class Migration(migrations.Migration):
    dependencies = [("theses", "0015_thesis_advisor_name_thesis_course_code_and_more")]

    operations = [migrations.RunPython(create_index, drop_index)]
