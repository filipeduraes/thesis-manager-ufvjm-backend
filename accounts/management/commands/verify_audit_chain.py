"""
Management command that verifies the integrity of the audit hash chain.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError, CommandParser

from accounts.audit_integrity import verify_audit_chain


class Command(BaseCommand):
    """Recompute every audit hash and report broken rows or links."""

    help = "Verifica a integridade da cadeia de hash do log de auditoria."

    def add_arguments(self, parser: CommandParser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """

    def handle(self, *args, **options) -> None:
        """
        Run the verification and fail when tampering is detected.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.

        Raises:
            CommandError: When broken rows or links are found.
        """
        report = verify_audit_chain()
        self.stdout.write(f"Linhas verificadas: {report.checked}")
        if report.ok:
            self.stdout.write(self.style.SUCCESS("Cadeia de auditoria íntegra."))
            return
        self.stdout.write(f"Hashes divergentes: {len(report.broken_rows)}")
        self.stdout.write(f"Ligações divergentes: {len(report.broken_links)}")
        raise CommandError("A cadeia de auditoria foi alterada.")
