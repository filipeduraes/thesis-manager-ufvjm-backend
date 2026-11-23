"""
Remove domain data created by previous E2E runs from a real database.

Use only when tests were run against the application database by mistake. The
append-only audit log is intentionally NOT deleted: removing rows would break
its SHA-256 hash chain.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from academic.models import Course, Department
from accounts.models import User
from notifications.models import Notification
from theses.models import Defense, Thesis


class Command(BaseCommand):
    """Report and optionally delete E2E-created users, courses and theses."""

    help = "Remove dados de domínio criados por execuções E2E (use --apply)."

    def add_arguments(self, parser: object) -> None:
        """
        Register the command arguments.

        Args:
            parser: Django command argument parser.
        """
        parser.add_argument(  # type: ignore[attr-defined]
            "--apply",
            action="store_true",
            help="Executa a remoção. Sem esta opção, apenas mostra o que seria removido.",
        )

    def handle(self, *args: object, **options: object) -> None:
        """
        Print the affected rows and delete them when ``--apply`` is set.

        Args:
            *args: Positional command arguments.
            **options: Parsed command options, including ``apply``.
        """
        users = User.objects.filter(email__istartswith="e2e-")
        departments = Department.objects.filter(name__istartswith="Departamento E2E")
        courses = Course.objects.filter(Q(name__istartswith="Curso E2E") | Q(code__istartswith="E2E"))
        theses = Thesis.objects.filter(
            Q(title__icontains=" E2E ")
            | Q(course__in=courses)
            | Q(advisor__in=users)
            | Q(authors__author__in=users)
        ).distinct()
        defenses = Defense.objects.filter(thesis__in=theses)
        notifications = Notification.objects.filter(
            Q(user__in=users)
            | Q(external_invitation__email__istartswith="e2e-")
            | Q(subject__icontains="E2E")
        )

        self.stdout.write(f"Usuários E2E: {users.count()}")
        self.stdout.write(f"Departamentos E2E: {departments.count()}")
        self.stdout.write(f"Cursos E2E: {courses.count()}")
        self.stdout.write(f"Monografias E2E: {theses.count()}")
        self.stdout.write(f"Bancas E2E: {defenses.count()}")
        self.stdout.write(f"Notificações E2E: {notifications.count()}")
        self.stdout.write("Auditoria: não removida (tabela append-only com cadeia SHA-256).")

        if not options["apply"]:
            self.stdout.write("Dry-run. Use --apply para remover.")
            return

        notifications.delete()
        defenses.delete()
        theses.delete()
        courses.delete()
        departments.delete()
        users.delete()
        self.stdout.write(self.style.SUCCESS("Remoção concluída."))
