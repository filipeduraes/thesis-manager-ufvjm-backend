"""Merge the external-invitation branch with the workflow-templates branch."""

from django.db import migrations


class Migration(migrations.Migration):
    """Join both `notifications` leaves into a single migration head."""

    dependencies = [
        ("notifications", "0004_notification_status_cancelled"),
        ("notifications", "0005_seed_workflow_templates"),
    ]

    operations = []
