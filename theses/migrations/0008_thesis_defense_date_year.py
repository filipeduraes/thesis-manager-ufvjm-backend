"""
Adiciona data/ano de defesa à monografia e preenche os valores das já publicadas.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import migrations, models

REPOSITORY_TIME_ZONE = ZoneInfo("America/Sao_Paulo")


def _local_date(value: datetime):
    """
    Converte um datetime com fuso para a data local de America/Sao_Paulo.
    """
    return value.astimezone(REPOSITORY_TIME_ZONE).date()


def backfill_defense_dates(apps, schema_editor) -> None:
    """
    Preenche `defense_date`/`defense_year` das monografias já publicadas.

    Usa a mesma ordem de prioridade do serviço de publicação: última banca
    concluída, depois o registro manual de resultado e, por fim, a data de
    publicação.
    """
    thesis_model = apps.get_model("theses", "Thesis")
    defense_model = apps.get_model("theses", "Defense")
    history_model = apps.get_model("theses", "ThesisStatusHistory")
    published = thesis_model.objects.filter(published_at__isnull=False, defense_date__isnull=True)
    for thesis in published.iterator():
        defense = defense_model.objects.filter(thesis_id=thesis.pk, status="COMPLETED").order_by("-scheduled_datetime").first()
        if defense is not None:
            defense_date = _local_date(defense.scheduled_datetime)
        else:
            result_entry = history_model.objects.filter(thesis_id=thesis.pk, metadata__has_key="defenseResult").order_by("-changed_at").first()
            source = result_entry.changed_at if result_entry is not None else thesis.published_at
            defense_date = _local_date(source)
        thesis_model.objects.filter(pk=thesis.pk).update(defense_date=defense_date, defense_year=defense_date.year)


class Migration(migrations.Migration):

    dependencies = [
        ("theses", "0007_uniq_institutional_member_per_defense"),
    ]

    operations = [
        migrations.AddField(
            model_name="thesis",
            name="defense_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="thesis",
            name="defense_year",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="thesis",
            index=models.Index(fields=["is_public", "defense_year"], name="thesis_public_year_idx"),
        ),
        migrations.RunPython(backfill_defense_dates, migrations.RunPython.noop),
    ]
