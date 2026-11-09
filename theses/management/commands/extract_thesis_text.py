"""
Management command that processes pending thesis text extractions.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from theses.tasks import extract_pending_thesis_text


class Command(BaseCommand):
    """
    Extract text from pending thesis PDFs.
    """

    help = "Processa a extração de texto de PDFs pendentes da fila de arquivos."

    def add_arguments(self, parser) -> None:
        """
        Register command arguments.

        Args:
            parser: Argument parser.
        """
        parser.add_argument("--limit", type=int, default=10, help="Máximo de arquivos processados nesta execução.")

    def handle(self, *args, **options) -> None:
        """
        Execute the extraction batch.

        Args:
            *args: Positional arguments forwarded by Django.
            **options: Parsed command options.
        """
        result = extract_pending_thesis_text(limit=options["limit"])
        self.stdout.write(f"Extração concluída: {result['completed']} concluído(s), {result['failed']} falha(s).")
