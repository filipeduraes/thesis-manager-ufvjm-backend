#!/usr/bin/env bash
# Roda a suíte Django contra o PostgreSQL real em modo dividido (6 bancos).
#
# Uso:
#   ./scripts/test-postgres.sh                 # suíte completa + cleanup
#   ./scripts/test-postgres.sh theses.tests    # só um app/módulo
#   ./scripts/test-postgres.sh --keepdb ...    # repassa opções ao manage.py test
#
# As URLs usam os defaults do docker-compose.yml (porta 5433). Sobrescreva
# POSTGRES_HOST/POSTGRES_USER/POSTGRES_PASSWORD se o seu ambiente for diferente.
set -euo pipefail

cd "$(dirname "$0")/.."

export DJANGO_SETTINGS_MODULE=thesismanagerbackend.settings
export SERVICE_DATABASES_SHARED=False

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

# Garante que os bancos existam e estejam migrados antes de testar.
.venv/bin/python manage.py ensure_service_databases >/dev/null
for alias in auth default committee repository reports notifications; do
  .venv/bin/python manage.py migrate --database "$alias" --noinput >/dev/null
done

.venv/bin/python manage.py test "$@"
