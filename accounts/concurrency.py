"""
Pequenas primitivas de concorrência usadas pelas regras de integridade.

O banco de produção é PostgreSQL (READ COMMITTED). Locks transacionais
"pg_advisory_xact_lock" serializam seções críticas que não podem ser protegidas
por uma simples linha (por exemplo, a cauda da cadeia de hash da auditoria ou a
contagem de administradores ativos). Em SQLite (usado apenas em testes E2E
isolados) não há equivalente, então a função não faz nada — a limitação é
documentada e os testes de concorrência relevantes rodam contra PostgreSQL.
"""
from django.db import connection

# Chaves arbitrárias e estáveis (derived from ASCII strings), distintas entre si.
AUDIT_CHAIN_LOCK_KEY = 0x41554449544C4F47  # "AUDITLOG"
ADMIN_ROLE_LOCK_KEY = 0x41444D494E524F4C  # "ADMINROL"
COURSE_ASSIGNMENT_LOCK_KEY = 0x41535349474E4D54  # "ASSIGNMT"
THESIS_DRAFT_LOCK_KEY = 0x54484553495344  # "THESISD"


def acquire_transaction_lock(key: int) -> None:
    """
    Serializa a seção crítica atual dentro de uma transação.

    Deve ser chamada dentro de ``transaction.atomic()``. O lock é liberado
    automaticamente no commit/rollback da transação.
    """
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])
