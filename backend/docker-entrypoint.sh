#!/usr/bin/env bash
# Wait for PostgreSQL, apply migrations, then run the given command (gunicorn by default).
set -euo pipefail
for i in $(seq 1 30); do
  pg_isready -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "${DB_USER:-trackable}" -q && break
  echo "waiting for database ($i)..."; sleep 2
done
if [[ "${RUN_MIGRATIONS:-true}" == "true" ]]; then
  python manage.py migrate --noinput
fi
exec "$@"
