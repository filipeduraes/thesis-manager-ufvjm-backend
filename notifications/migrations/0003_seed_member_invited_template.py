"""
Seed the e-mail template used to invite a committee member to a defense.
"""

from django.db import migrations

CODE = "DEFENSE_MEMBER_INVITED"
SUBJECT = "Convite para banca de defesa: {{ thesis_title }}"
VARIABLES = ["recipient_name", "thesis_title", "reference_code", "action_url", "scheduled_at"]
BODY_TEXT = (
    "Olá, {{ recipient_name }}.\n\n"
    "Você foi convidado(a) para compor a banca de defesa da monografia "
    "\"{{ thesis_title }}\" ({{ reference_code }}), agendada para {{ scheduled_at }}.\n\n"
    "Acesse o sistema para confirmar sua disponibilidade e, no momento da defesa, registrar sua avaliação.\n\n"
    "Acesse o sistema: {{ action_url }}\n\n"
    "Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n"
)
BODY_HTML = (
    "<p>Olá, {{ recipient_name }}.</p>"
    "<p>Você foi convidado(a) para compor a banca de defesa da monografia "
    "\"{{ thesis_title }}\" ({{ reference_code }}), agendada para {{ scheduled_at }}.</p>"
    "<p>Acesse o sistema para confirmar sua disponibilidade e registrar sua avaliação.</p>"
    '<p><a href="{{ action_url }}">Acessar o sistema de TCC</a></p>'
    '<p style="color:#666;font-size:12px">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>'
)


def seed_template(apps, schema_editor) -> None:
    """
    Insert the committee invitation template.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    template_model = apps.get_model("notifications", "NotificationTemplate")
    template_model.objects.update_or_create(
        code=CODE,
        defaults={"subject": SUBJECT, "variables": VARIABLES, "body_text": BODY_TEXT, "body_html": BODY_HTML, "is_active": True},
    )


def remove_template(apps, schema_editor) -> None:
    """
    Remove the template when the migration is reversed.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    apps.get_model("notifications", "NotificationTemplate").objects.filter(code=CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_seed_templates"),
    ]

    operations = [
        migrations.RunPython(seed_template, remove_template),
    ]
