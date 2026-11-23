#!/usr/bin/env bash
# Roda a suíte Django contra o PostgreSQL real em modo dividido (6 bancos).
#
# Uso:
#   ./scripts/test-postgres.sh                 # completa + cleanup
#   ./scripts/test-postgres.sh theses.tests    # só um app/módulo
#   ./scripts/test-postgres.sh --keepdb ...    # repassa opções ao manage.py test
#
# Se o Postgres do Compose (porta 5433) não estiver no ar, o script sobe só o
# serviço `db`. Use POSTGRES_* para apontar para outro servidor.
set -euo pipefail

cd "$(dirname "$0")/.."

export DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings
export SERVICE_DATABASES_SHARED=False
# Os testes usam mail.outbox; locmem evita o aviso W001 de e-mail no log.
export EMAIL_BACKEND=django.core.mail.backends.locmem.EmailBackend

HOST="${POSTGRES_HOST:-127.0.0.1}"
PORT="${POSTGRES_HOST_PORT:-5433}"
USER="${POSTGRES_USER:-thesis_manager_user}"
PASSWORD="${POSTGRES_PASSWORD:-local-development-only}"
BASE="postgres://${USER}:${PASSWORD}@${HOST}:${PORT}"

export DATABASE_URL="${BASE}/thesis_manager"
export AUTH_DATABASE_URL="${BASE}/thesis_auth"
export COMMITTEE_DATABASE_URL="${BASE}/thesis_committee"
export REPOSITORY_DATABASE_URL="${BASE}/thesis_repository"
export REPORTS_DATABASE_URL="${BASE}/thesis_reports"
export NOTIFICATIONS_DATABASE_URL="${BASE}/thesis_notifications"

pg_ready() {
  .venv/bin/python - "$HOST" "$PORT" "$USER" "$PASSWORD" <<'PY' >/dev/null 2>&1
import sys
import psycopg
host, port, user, password = sys.argv[1:5]
try:
    with psycopg.connect(dbname="postgres", user=user, password=password, host=host, port=port, connect_timeout=2):
        pass
except Exception:
    sys.exit(1)
PY
}

if ! pg_ready; then
  echo "Postgres não responde em ${HOST}:${PORT}. Subindo o serviço db do Compose..."
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  else
    DOCKER=(sudo docker)
  fi
  "${DOCKER[@]}" compose up -d db

  echo "Aguardando Postgres..."
  for _ in $(seq 1 60); do
    pg_ready && break
    sleep 2
  done
  if ! pg_ready; then
    echo "Falha: Postgres não ficou pronto em ${HOST}:${PORT}." >&2
    exit 1
  fi
fi

# Garante que os bancos existam e estejam migrados antes de testar.
.venv/bin/python manage.py ensure_service_databases >/dev/null
for alias in auth default committee repository reports notifications; do
  .venv/bin/python manage.py migrate --database "$alias" --noinput >/dev/null
done

.venv/bin/python manage.py test "$@"
