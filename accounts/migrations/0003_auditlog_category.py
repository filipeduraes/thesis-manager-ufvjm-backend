from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_auditlog_actor_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="category",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]