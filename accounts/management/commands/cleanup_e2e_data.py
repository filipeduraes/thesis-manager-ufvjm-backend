"""
Emergency cleanup of E2E/smoke data that reached a real database.

With a separate smoke stack this should never be needed; keep it for mistakes.
The append-only audit log is intentionally left untouched (SHA-256 chain).
Deletes use ``_raw_delete`` on each owning alias so Django's collector never
emits a cross-database JOIN for SET_NULL/cascade.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandParser
from django.db.models import Q, QuerySet

from academic.models import Course, Department, UserCourseAssignment
from accounts.concurrency import database_for_model
from accounts.models import ExternalInvitation, User, UserProfileRole
from events.models import OutboxEvent, ProcessedEvent
from notifications.models import Notification
from reports_readmodel.models import ReportFact
from repository_readmodel.models import PublicAdvisor, PublicCourse, PublicThesis
from theses.models import (
    Defense,
    DefenseMember,
    DefenseMemberEvaluation,
    DefenseStatusHistory,
    Thesis,
    ThesisAuthor,
    ThesisDocument,
    ThesisFile,
    ThesisKeyword,
    ThesisReview,
    ThesisStatusHistory,
)


class Command(BaseCommand):
    """Report and optionally delete E2E-created domain data."""

    help = "Remove dados de domínio criados por execuções E2E/smoke (use --apply)."

    def add_arguments(self, parser: CommandParser) -> None:
        """Register the command arguments."""
        parser.add_argument("--apply", action="store_true", help="Executa a remoção; sem isto, apenas mostra o que seria removido.")

    @staticmethod
    def _delete(queryset: QuerySet) -> int:
        """Delete rows with no collector, on the queryset's own alias."""
        alias = queryset.db
        ids = list(queryset.values_list("pk", flat=True))
        if not ids:
            return 0
        return queryset.model.objects.using(alias).filter(pk__in=ids)._raw_delete(alias)

    def handle(self, *args: object, **options: object) -> None:
        """Print the affected rows and delete them when ``--apply`` is set."""
        auth_db = database_for_model(User)
        thesis_db = database_for_model(Thesis)
        notification_db = database_for_model(Notification)
        invitation_db = database_for_model(ExternalInvitation)

        users = User.objects.using(auth_db).filter(
            Q(email__istartswith="e2e-") | Q(email__istartswith="convidado-smoke-") | Q(name__icontains="E2E") | Q(name__icontains="Smoke")
        )
        user_ids = list(users.values_list("pk", flat=True))
        departments = Department.objects.using(auth_db).filter(name__istartswith="Departamento E2E")
        department_ids = list(departments.values_list("pk", flat=True))
        courses = Course.objects.using(auth_db).filter(Q(name__istartswith="Curso E2E") | Q(code__istartswith="E2E"))
        course_ids = list(courses.values_list("pk", flat=True))
        invitations = ExternalInvitation.objects.using(invitation_db).filter(
            Q(email__istartswith="e2e-") | Q(email__istartswith="convidado-smoke-") | Q(name__icontains="E2E") | Q(name__icontains="Smoke")
        )
        invitation_ids = list(invitations.values_list("pk", flat=True))

        theses = Thesis.objects.using(thesis_db).filter(
            Q(title__icontains="E2E") | Q(title__istartswith="Smoke real") | Q(course_id__in=course_ids) | Q(advisor_id__in=user_ids)
        )
        thesis_ids = list(theses.values_list("pk", flat=True))
        defense_ids = list(Defense.objects.using(thesis_db).filter(thesis_id__in=thesis_ids).values_list("pk", flat=True))
        member_ids = list(DefenseMember.objects.using(thesis_db).filter(defense_id__in=defense_ids).values_list("pk", flat=True))
        file_ids = list(ThesisFile.objects.using(thesis_db).filter(thesis_id__in=thesis_ids).values_list("pk", flat=True))
        notifications = Notification.objects.using(notification_db).filter(
            Q(user_id__in=user_ids) | Q(external_invitation_id__in=invitation_ids) | Q(subject__icontains="E2E")
        )
        event_ids = list(OutboxEvent.objects.using(thesis_db).filter(aggregate_id__in=thesis_ids).values_list("pk", flat=True))

        self.stdout.write(f"Usuários: {len(user_ids)}")
        self.stdout.write(f"Departamentos: {len(department_ids)}")
        self.stdout.write(f"Cursos: {len(course_ids)}")
        self.stdout.write(f"Convites externos: {len(invitation_ids)}")
        self.stdout.write(f"Monografias: {len(thesis_ids)}")
        self.stdout.write(f"Bancas: {len(defense_ids)}")
        self.stdout.write(f"Notificações: {notifications.count()}")
        self.stdout.write("Auditoria: não removida (cadeia SHA-256). Usuários/convites preservados.")

        if not options["apply"]:
            self.stdout.write("Dry-run. Use --apply para remover.")
            return

        self._delete(notifications)
        self._delete(ProcessedEvent.objects.using(thesis_db).filter(event_id__in=event_ids))
        self._delete(OutboxEvent.objects.using(thesis_db).filter(pk__in=event_ids))
        self._delete(DefenseMemberEvaluation.objects.using(thesis_db).filter(member_id__in=member_ids))
        self._delete(DefenseMember.objects.using(thesis_db).filter(defense_id__in=defense_ids))
        self._delete(DefenseStatusHistory.objects.using(thesis_db).filter(defense_id__in=defense_ids))
        self._delete(Defense.objects.using(thesis_db).filter(pk__in=defense_ids))
        self._delete(ThesisStatusHistory.objects.using(thesis_db).filter(thesis_id__in=thesis_ids))
        self._delete(ThesisAuthor.objects.using(thesis_db).filter(thesis_id__in=thesis_ids))
        self._delete(ThesisKeyword.objects.using(thesis_db).filter(thesis_id__in=thesis_ids))
        self._delete(ThesisFile.objects.using(thesis_db).filter(thesis_id__in=thesis_ids))
        self._delete(ThesisDocument.objects.using(thesis_db).filter(thesis_id__in=thesis_ids))
        self._delete(ThesisReview.objects.using(thesis_db).filter(thesis_file_id__in=file_ids))
        self._delete(PublicThesis.objects.filter(pk__in=thesis_ids))
        self._delete(ReportFact.objects.filter(thesis_id__in=thesis_ids))
        self._delete(theses)
        self._delete(PublicCourse.objects.filter(pk__in=course_ids))
        self._delete(PublicAdvisor.objects.filter(pk__in=user_ids))
        self._delete(UserCourseAssignment.objects.using(auth_db).filter(user_id__in=user_ids))
        self._delete(UserProfileRole.objects.using(auth_db).filter(user_id__in=user_ids))
        self._delete(courses)
        self._delete(departments)
        # Usuários e convites são preservados: audit_log os referencia e a
        # cadeia SHA-256 não pode ser reescrita. O stack de smoke é descartável.
        self.stdout.write(self.style.SUCCESS("Remoção concluída (usuários e convites preservados pela auditoria)."))
