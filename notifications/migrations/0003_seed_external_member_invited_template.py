"""
Seed the e-mail template that carries an external committee member's access token.

The token itself is never stored: the consumer renders the stored copy with a
masked value and sends the real token only in the outgoing message.
"""

from django.db import migrations

TEMPLATE_CODE = "EXTERNAL_MEMBER_INVITED"

TEMPLATE = {
    "subject": "Convite para banca de TCC: {{ thesis_title }}",
    "variables": ["recipient_name", "thesis_title", "reference_code", "action_url", "role_label", "scheduled_at", "access_token", "expires_at"],
    "body_text": (
        "Olá, {{ recipient_name }}.\n\n"
        "Você foi convidado(a) a participar, como {{ role_label }}, da banca da monografia "
        "\"{{ thesis_title }}\" ({{ reference_code }}), agendada para {{ scheduled_at }}.\n\n"
        "Seu código de acesso é:\n\n"
        "    {{ access_token }}\n\n"
        "Ele é válido até {{ expires_at }} (horário de Brasília) e substitui qualquer código enviado antes.\n\n"
        "Como acessar:\n"
        "1. Abra {{ action_url }}.\n"
        "2. Em \"Sou membro de banca externo\", informe o código acima.\n"
        "3. O código funciona uma única vez e abre uma sessão temporária, "
        "onde você confirma sua disponibilidade, consulta o local ou link da sessão e registra sua avaliação.\n\n"
        "Não compartilhe este código. Se a sessão expirar ou o código deixar de funcionar, "
        "peça um novo convite à coordenação ou ao(à) orientador(a) do trabalho.\n\n"
        "Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n"
    ),
    "body_html": (
        "<p>Olá, {{ recipient_name }}.</p>"
        "<p>Você foi convidado(a) a participar, como {{ role_label }}, da banca da monografia "
        "<strong>{{ thesis_title }}</strong> ({{ reference_code }}), agendada para {{ scheduled_at }}.</p>"
        "<p>Seu código de acesso é:</p>"
        '<p style="font-family:monospace;font-size:18px;letter-spacing:1px"><strong>{{ access_token }}</strong></p>'
        "<p>Ele é válido até {{ expires_at }} (horário de Brasília) e substitui qualquer código enviado antes.</p>"
        "<p>Como acessar:</p>"
        "<ol>"
        '<li>Abra <a href="{{ action_url }}">{{ action_url }}</a>.</li>'
        "<li>Em <strong>Sou membro de banca externo</strong>, informe o código acima.</li>"
        "<li>O código funciona uma única vez e abre uma sessão temporária, "
        "onde você confirma sua disponibilidade, consulta o local ou link da sessão e registra sua avaliação.</li>"
        "</ol>"
        "<p>Não compartilhe este código. Se a sessão expirar ou o código deixar de funcionar, "
        "peça um novo convite à coordenação ou ao(à) orientador(a) do trabalho.</p>"
        '<p style="color:#666;font-size:12px">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>'
    ),
}


def seed_template(apps, schema_editor) -> None:
    """
    Create or update the external member invitation template.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    notification_template_model = apps.get_model("notifications", "NotificationTemplate")
    defaults = {**TEMPLATE, "is_active": True}
    notification_template_model.objects.update_or_create(code=TEMPLATE_CODE, defaults=defaults)


def remove_template(apps, schema_editor) -> None:
    """
    Delete the template when the migration is reversed.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    notification_template_model = apps.get_model("notifications", "NotificationTemplate")
    notification_template_model.objects.filter(code=TEMPLATE_CODE).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_seed_templates"),
    ]

    operations = [
        migrations.RunPython(seed_template, remove_template),
    ]
