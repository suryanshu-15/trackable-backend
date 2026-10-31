# Shared helper: load DB settings from backend/.env (if present) without overriding the environment.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT/backend/.env" ]]; then
  while IFS='=' read -r key value; do
    [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [[ -z "${!key:-}" ]] && export "$key=$value"
  done < <(grep -E '^[A-Z_][A-Z0-9_]*=' "$ROOT/backend/.env")
fi
export PGHOST="${DB_HOST:-localhost}" PGPORT="${DB_PORT:-5432}" PGUSER="${DB_USER:-trackable}" PGPASSWORD="${DB_PASSWORD:-}"
DB_NAME="${DB_NAME:-trackable}"
MEDIA_ROOT="${MEDIA_ROOT:-$ROOT/backend/mediafiles}"
