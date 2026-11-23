"""Seed templates for cancellation, rescheduling and advisor review events."""

from django.db import migrations

TEMPLATES = [
    {
        "code": "DEFENSE_CANCELLED",
        "subject": "Banca cancelada: {{ thesis_title }}",
        "variables": ["recipient_name", "thesis_title", "reference_code", "action_url"],
        "text": "Olá, {{ recipient_name }}.\n\nA banca de defesa da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi cancelada.\n\nAcesse o sistema: {{ action_url }}\n\nMensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n",
        "html": "<p>Olá, {{ recipient_name }}.</p><p>A banca de defesa da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi cancelada.</p><p><a href=\"{{ action_url }}\">Acessar o sistema de TCC</a></p><p style=\"color:#666;font-size:12px\">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>",
    },
    {
        "code": "DEFENSE_RESCHEDULED",
        "subject": "Banca remarcada: {{ thesis_title }}",
        "variables": ["recipient_name", "thesis_title", "reference_code", "action_url", "scheduled_at"],
        "text": "Olá, {{ recipient_name }}.\n\nA banca de defesa da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi remarcada para {{ scheduled_at }}.\n\nAcesse o sistema: {{ action_url }}\n\nMensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n",
        "html": "<p>Olá, {{ recipient_name }}.</p><p>A banca de defesa da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi remarcada para {{ scheduled_at }}.</p><p><a href=\"{{ action_url }}\">Acessar o sistema de TCC</a></p><p style=\"color:#666;font-size:12px\">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>",
    },
    {
        "code": "THESIS_REVIEW_RECORDED",
        "subject": "Parecer do orientador registrado: {{ thesis_title }}",
        "variables": ["recipient_name", "thesis_title", "reference_code", "action_url", "decision"],
        "text": "Olá, {{ recipient_name }}.\n\nO orientador registrou o parecer \"{{ decision }}\" na monografia \"{{ thesis_title }}\" ({{ reference_code }}).\n\nAcesse o sistema para acompanhar: {{ action_url }}\n\nMensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n",
        "html": "<p>Olá, {{ recipient_name }}.</p><p>O orientador registrou o parecer \"{{ decision }}\" na monografia \"{{ thesis_title }}\" ({{ reference_code }}).</p><p><a href=\"{{ action_url }}\">Acessar o sistema de TCC</a></p><p style=\"color:#666;font-size:12px\">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>",
    },
]


def seed_templates(apps, schema_editor) -> None:
    """Insert or update the workflow templates."""
    model = apps.get_model("notifications", "NotificationTemplate")
    for item in TEMPLATES:
        model.objects.update_or_create(
            code=item["code"],
            defaults={"subject": item["subject"], "variables": item["variables"], "body_text": item["text"], "body_html": item["html"], "is_active": True},
        )


def remove_templates(apps, schema_editor) -> None:
    """Remove the seeded templates when reversing."""
    apps.get_model("notifications", "NotificationTemplate").objects.filter(code__in=[item["code"] for item in TEMPLATES]).delete()


class Migration(migrations.Migration):
    dependencies = [("notifications", "0004_alter_notification_event_and_more")]
    operations = [migrations.RunPython(seed_templates, remove_templates)]
