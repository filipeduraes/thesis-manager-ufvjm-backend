"""
Tests for the seeded notification templates, their rendering and the notification constraints.
"""

from __future__ import annotations

import uuid

from django.db import IntegrityError, transaction
from django.template import TemplateSyntaxError
from django.test import TestCase

from accounts.models import User
from events.catalog import EventType
from events.outbox import AggregateRef, record_event
from notifications.models import Notification, NotificationTemplate, NotificationTemplateCode
from notifications.rendering import TemplateContextError, render_notification

COMMON_VARIABLES = {"recipient_name", "thesis_title", "reference_code", "action_url"}


def full_context(template: NotificationTemplate) -> dict[str, str | int]:
    return {variable: f"valor-{variable}" for variable in template.variables}


class SeededTemplatesTests(TestCase):
    """The data migration creates one active template per notified event."""

    def test_every_code_is_seeded_and_active(self) -> None:
        seeded_codes = set(NotificationTemplate.objects.filter(is_active=True).values_list("code", flat=True))
        self.assertEqual(seeded_codes, set(NotificationTemplateCode.values))

    def test_every_template_declares_common_variables_and_renders_completely(self) -> None:
        for template in NotificationTemplate.objects.all():
            with self.subTest(code=template.code):
                self.assertTrue(COMMON_VARIABLES <= set(template.variables))
                rendered = render_notification(template, full_context(template))
                for variable in template.variables:
                    self.assertIn(f"valor-{variable}", rendered.body_text + rendered.body_html + rendered.subject)
                for content in (rendered.subject, rendered.body_text, rendered.body_html):
                    self.assertNotIn("{{", content)
                    self.assertNotIn("{%", content)

    def test_templates_never_reference_tokens_or_secrets(self) -> None:
        for template in NotificationTemplate.objects.all():
            source = f"{template.subject} {template.body_text} {template.body_html} {template.variables}".lower()
            with self.subTest(code=template.code):
                self.assertNotIn("token", source)
                self.assertNotIn("senha", source)


class RenderNotificationTests(TestCase):
    """Rendering escapes HTML, keeps the subject on one line and requires every variable."""

    def setUp(self) -> None:
        self.template = NotificationTemplate.objects.get(code=NotificationTemplateCode.DEFENSE_COMPLETED)

    def test_html_body_escapes_variables_and_text_body_keeps_them(self) -> None:
        context = full_context(self.template)
        context["thesis_title"] = '<script>alert("x")</script> & Cia'
        rendered = render_notification(self.template, context)
        self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; Cia", rendered.body_html)
        self.assertNotIn("<script>", rendered.body_html)
        self.assertIn('<script>alert("x")</script> & Cia', rendered.body_text)

    def test_subject_is_collapsed_to_single_line(self) -> None:
        context = full_context(self.template)
        context["thesis_title"] = "Título\r\nBcc: alguem@example.com"
        rendered = render_notification(self.template, context)
        self.assertNotIn("\n", rendered.subject)
        self.assertNotIn("\r", rendered.subject)

    def test_missing_variable_raises(self) -> None:
        context = full_context(self.template)
        del context["result_label"]
        with self.assertRaises(TemplateContextError):
            render_notification(self.template, context)

    def test_template_cannot_load_project_libraries(self) -> None:
        unsafe = NotificationTemplate(code="UNSAFE", subject="x", body_text="{% load static %}x", body_html="x", variables=[])
        with self.assertRaises(TemplateSyntaxError):
            render_notification(unsafe, {})


class NotificationConstraintTests(TestCase):
    """The database rejects duplicate deliveries per event and notifications without recipient."""

    def setUp(self) -> None:
        self.template = NotificationTemplate.objects.get(code=NotificationTemplateCode.THESIS_PUBLISHED)
        self.user = User.objects.create_user(email="aluno@ufvjm.edu.br", name="Aluno")
        thesis_id = uuid.uuid4()
        payload = {"thesisId": str(thesis_id), "referenceCode": "TCC-1", "publishedAt": "2026-11-21T12:00:00+00:00"}
        with transaction.atomic():
            self.event = record_event(EventType.THESIS_PUBLISHED, AggregateRef("Thesis", thesis_id), payload)

    def _create(self, **recipient: object) -> Notification:
        return Notification.objects.create(template=self.template, event=self.event, subject="Assunto", body="Corpo", **recipient)

    def test_same_user_cannot_receive_same_event_twice(self) -> None:
        self._create(user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create(user=self.user)

    def test_same_external_address_cannot_receive_same_event_twice(self) -> None:
        self._create(external_email="externo@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create(external_email="externo@example.com")

    def test_different_recipients_of_same_event_are_allowed(self) -> None:
        self._create(user=self.user)
        self._create(external_email="externo@example.com")
        self.assertEqual(Notification.objects.filter(event=self.event).count(), 2)

    def test_notifications_without_event_are_not_deduplicated(self) -> None:
        Notification.objects.create(template=self.template, user=self.user, subject="A", body="B")
        Notification.objects.create(template=self.template, user=self.user, subject="A", body="B")
        self.assertEqual(Notification.objects.filter(event__isnull=True).count(), 2)

    def test_notification_requires_a_recipient(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._create()
