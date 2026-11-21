"""
Seed the notification templates used by the event consumer.

The texts live in this migration (not imported from the app) so that the
migration keeps producing the same rows even if the application code changes.
"""

from django.db import migrations

COMMON_VARIABLES = ["recipient_name", "thesis_title", "reference_code", "action_url"]

HTML_FOOTER = '<p><a href="{{ action_url }}">Acessar o sistema de TCC</a></p><p style="color:#666;font-size:12px">Mensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.</p>'
TEXT_FOOTER = "Acesse o sistema: {{ action_url }}\n\nMensagem automática do Sistema de TCC da UFVJM. Não responda este e-mail.\n"

TEMPLATES = [
    {
        "code": "THESIS_SUBMITTED",
        "subject": "Nova submissão de monografia: {{ thesis_title }}",
        "variables": COMMON_VARIABLES + ["author_names", "version_number"],
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "{{ author_names }} submeteu a versão {{ version_number }} da monografia "
            "\"{{ thesis_title }}\" ({{ reference_code }}) para a sua avaliação como orientador(a).\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>{{ author_names }} submeteu a versão {{ version_number }} da monografia "
            "<strong>{{ thesis_title }}</strong> ({{ reference_code }}) para a sua avaliação como orientador(a).</p>"
        ),
    },
    {
        "code": "DEFENSE_SCHEDULED",
        "subject": "Banca agendada: {{ thesis_title }}",
        "variables": COMMON_VARIABLES + ["scheduled_at", "duration_minutes", "modality_label"],
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "A banca da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi agendada para "
            "{{ scheduled_at }}, com duração prevista de {{ duration_minutes }} minutos, na modalidade {{ modality_label }}.\n"
            "O local ou o link da sessão está disponível no sistema.\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>A banca da monografia <strong>{{ thesis_title }}</strong> ({{ reference_code }}) foi agendada para "
            "<strong>{{ scheduled_at }}</strong>, com duração prevista de {{ duration_minutes }} minutos, na modalidade {{ modality_label }}.</p>"
            "<p>O local ou o link da sessão está disponível no sistema.</p>"
        ),
    },
    {
        "code": "DEFENSE_EVALUATION_RECORDED",
        "subject": "Avaliação registrada na banca: {{ thesis_title }}",
        "variables": COMMON_VARIABLES + ["evaluations_submitted", "evaluations_required"],
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "Um membro da banca da monografia \"{{ thesis_title }}\" ({{ reference_code }}) registrou a sua avaliação. "
            "Avaliações registradas: {{ evaluations_submitted }} de {{ evaluations_required }}.\n"
            "O resultado será divulgado quando a banca for encerrada.\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>Um membro da banca da monografia <strong>{{ thesis_title }}</strong> ({{ reference_code }}) registrou a sua avaliação. "
            "Avaliações registradas: {{ evaluations_submitted }} de {{ evaluations_required }}.</p>"
            "<p>O resultado será divulgado quando a banca for encerrada.</p>"
        ),
    },
    {
        "code": "DEFENSE_COMPLETED",
        "subject": "Banca concluída: {{ thesis_title }}",
        "variables": COMMON_VARIABLES + ["result_label"],
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "A banca da monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi encerrada.\n"
            "Resultado: {{ result_label }}.\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>A banca da monografia <strong>{{ thesis_title }}</strong> ({{ reference_code }}) foi encerrada.</p>"
            "<p>Resultado: <strong>{{ result_label }}</strong>.</p>"
        ),
    },
    {
        "code": "THESIS_DEFENSE_RESULT_RECORDED",
        "subject": "Resultado da banca registrado: {{ thesis_title }}",
        "variables": COMMON_VARIABLES + ["result_label"],
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "A coordenação registrou o resultado da banca da monografia \"{{ thesis_title }}\" ({{ reference_code }}).\n"
            "Resultado: {{ result_label }}.\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>A coordenação registrou o resultado da banca da monografia <strong>{{ thesis_title }}</strong> ({{ reference_code }}).</p>"
            "<p>Resultado: <strong>{{ result_label }}</strong>.</p>"
        ),
    },
    {
        "code": "THESIS_PUBLISHED",
        "subject": "Monografia publicada no repositório: {{ thesis_title }}",
        "variables": COMMON_VARIABLES,
        "body_text": (
            "Olá, {{ recipient_name }}.\n\n"
            "A monografia \"{{ thesis_title }}\" ({{ reference_code }}) foi publicada no repositório institucional "
            "e já está disponível para consulta pública.\n\n"
        ),
        "body_html": (
            "<p>Olá, {{ recipient_name }}.</p>"
            "<p>A monografia <strong>{{ thesis_title }}</strong> ({{ reference_code }}) foi publicada no repositório institucional "
            "e já está disponível para consulta pública.</p>"
        ),
    },
]


def seed_templates(apps, schema_editor) -> None:
    """
    Create or update the notification templates.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    notification_template_model = apps.get_model("notifications", "NotificationTemplate")
    for template in TEMPLATES:
        defaults = {
            "subject": template["subject"],
            "variables": template["variables"],
            "body_text": template["body_text"] + TEXT_FOOTER,
            "body_html": template["body_html"] + HTML_FOOTER,
            "is_active": True,
        }
        notification_template_model.objects.update_or_create(code=template["code"], defaults=defaults)


def remove_templates(apps, schema_editor) -> None:
    """
    Delete the seeded templates when the migration is reversed.

    Args:
        apps: Historical app registry.
        schema_editor: Schema editor (unused).
    """
    notification_template_model = apps.get_model("notifications", "NotificationTemplate")
    codes = [template["code"] for template in TEMPLATES]
    notification_template_model.objects.filter(code__in=codes).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_templates, remove_templates),
    ]
