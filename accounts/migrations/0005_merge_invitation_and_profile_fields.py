"""Merge the token-delivery/audit branch with the user profile fields branch."""

from django.db import migrations


class Migration(migrations.Migration):
    """Join both `accounts` leaves into a single migration head."""

    dependencies = [
        ("accounts", "0004_merge_token_delivery_audit_category"),
        ("accounts", "0004_user_department_user_registration_number_and_more"),
    ]

    operations = []
