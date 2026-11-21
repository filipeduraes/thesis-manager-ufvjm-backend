"""
Rendering of notification templates with the Django template language.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.template import Context, Engine

from notifications.models import NotificationTemplate

TemplateContextValue = str | int | None
TemplateContext = dict[str, TemplateContextValue]

# Engine isolado: sem loaders nem context processors, os modelos vindos do banco
# não conseguem incluir arquivos do projeto nem ler request/usuário.
_ENGINE = Engine(debug=False, loaders=[], libraries={}, builtins=[])


class TemplateContextError(ValueError):
    """
    Raised when the context lacks variables declared by the template.
    """


@dataclass(frozen=True)
class RenderedMessage:
    """
    Rendered notification content.

    Attributes:
        subject: Single-line subject.
        body_text: Plain text body.
        body_html: HTML body with every variable escaped.
    """

    subject: str
    body_text: str
    body_html: str


def _render(source: str, context: TemplateContext, autoescape: bool) -> str:
    """
    Render one template string.

    Args:
        source: Template source.
        context: Template variables.
        autoescape: Whether variables are HTML-escaped.

    Returns:
        str: Rendered text.
    """
    template = _ENGINE.from_string(source)
    return template.render(Context(context, autoescape=autoescape))


def render_notification(template: NotificationTemplate, context: TemplateContext) -> RenderedMessage:
    """
    Render subject, text and HTML bodies of a template.

    Args:
        template: Template to render.
        context: Values for the variables listed in ``template.variables``.

    Returns:
        RenderedMessage: Rendered content; the subject is collapsed to one line.

    Raises:
        TemplateContextError: When a declared variable is missing from the context.
    """
    missing = sorted(set(template.variables) - set(context))
    if missing:
        raise TemplateContextError(f"Template {template.code} is missing variables: {', '.join(missing)}")
    raw_subject = _render(template.subject, context, autoescape=False)
    subject = " ".join(raw_subject.split())
    body_text = _render(template.body_text, context, autoescape=False)
    body_html = _render(template.body_html, context, autoescape=True)
    return RenderedMessage(subject=subject, body_text=body_text, body_html=body_html)
