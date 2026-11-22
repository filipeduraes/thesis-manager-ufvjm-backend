"""
Fixtures shared by the external member invitation e-mail tests.
"""

from __future__ import annotations

from datetime import timedelta

from django.core import mail
from django.core.mail.backends.locmem import EmailBackend as LocmemEmailBackend
from django.db import transaction
from django.utils import timezone
from rest_framework.test import APIClient

from academic.models import Course, Department
from accounts.models import ExternalInvitation, ProfileRole, User, UserProfileRole
from accounts.services import create_external_invitation
from events.catalog import ExternalInvitationTrigger
from events.envelope import EventEnvelope
from events.models import OutboxEvent
from theses.defense_scheduling import schedule_defense
from theses.events import record_external_member_invited
from theses.models import CommitteeRole, DefenseMember, DefenseModality, Thesis, ThesisAuthor, ThesisStatus

TEST_CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class EchoingFailingEmailBackend(LocmemEmailBackend):
    """
    E-mail backend that fails with an error echoing the message body, like some SMTP relays.
    """

    def send_messages(self, email_messages: list) -> int:
        raise RuntimeError(f"relay refused message: {email_messages[0].body}")


def create_user(email: str, name: str, role: str) -> User:
    user = User.objects.create_user(email=email, name=name)
    UserProfileRole.objects.create(user=user, role=role)
    return user


def envelope_for(event: OutboxEvent) -> EventEnvelope:
    body = EventEnvelope.from_outbox(event).to_json_bytes()
    return EventEnvelope.from_json_bytes(body)


INVITATION_TEST_SETTINGS = {"CACHES": TEST_CACHES, "SESSION_ENGINE": "django.contrib.sessions.backends.db", "FRONTEND_URL": "https://tcc.example.com"}


class InvitationFixtureMixin:
    """
    Scheduled defense with one external member, plus helpers to record and exchange invitations.
    """

    def setUp(self) -> None:
        department = Department.objects.create(name="Computação")
        self.admin = create_user("admin@ufvjm.edu.br", "Admin", ProfileRole.ADMIN)
        course = Course.objects.create(department=department, name="CC", code="CC001")
        student = create_user("aluno@ufvjm.edu.br", "Ana Aluna", ProfileRole.STUDENT)
        advisor = create_user("orientador@ufvjm.edu.br", "Olga Orientadora", ProfileRole.ADVISOR)
        self.thesis = Thesis.objects.create(course=course, advisor=advisor, title="Filas & <Eventos>", status=ThesisStatus.ADVISOR_APPROVED)
        ThesisAuthor.objects.create(thesis=self.thesis, author=student, position=1, is_main_author=True)
        self.defense = schedule_defense(self.thesis, scheduled_datetime=timezone.now() + timedelta(days=7), modality=DefenseModality.REMOTE, location="https://meet.example.com/sala", actor=self.admin)
        invitation, _unused = create_external_invitation(name="Edu Externo", email="externo@example.org", institution="UFMG", invited_by=self.admin)
        self.member = DefenseMember.objects.create(defense=self.defense, role=CommitteeRole.MEMBER, external_invitation=invitation, name="Edu Externo", email="externo@example.org", institution="UFMG")
        mail.outbox.clear()

    def _record(self, trigger: ExternalInvitationTrigger = ExternalInvitationTrigger.CREATED) -> OutboxEvent:
        with transaction.atomic():
            member = DefenseMember.objects.select_related("external_invitation").get(pk=self.member.pk)
            return record_external_member_invited(self.defense, member, trigger)

    def _invitation(self) -> ExternalInvitation:
        return ExternalInvitation.objects.get(pk=self.member.external_invitation_id)

    def _exchange_status(self, token: str) -> int:
        client = APIClient()
        response = client.post("/api/v1/auth/external-token", {"token": token}, format="json")
        return response.status_code
