#!/usr/bin/env bash
# Restore a database dump (and optionally media) produced by backup.sh.
# Usage: scripts/restore.sh <db.dump> [media.tar.gz]
#   TARGET_DB=<name>   restore into another database (created if missing)
#   CONFIRM=yes        required when restoring over the live database ($DB_NAME)
set -euo pipefail
source "$(dirname "$0")/_env.sh"
DUMP="${1:?usage: restore.sh <db.dump> [media.tar.gz]}"
MEDIA="${2:-}"
TARGET="${TARGET_DB:-$DB_NAME}"

if [[ "$TARGET" == "$DB_NAME" && "${CONFIRM:-}" != "yes" ]]; then
  echo "Refusing to overwrite the live database '$DB_NAME'. Stop the application, then re-run with CONFIRM=yes." >&2
  exit 2
fi
pg_restore --list "$DUMP" > /dev/null
if ! psql -d postgres -Atc "select 1 from pg_database where datname='$TARGET'" | grep -q 1; then
  createdb "$TARGET"
fi
pg_restore --clean --if-exists --no-owner --exit-on-error --dbname="$TARGET" "$DUMP"
echo "database restored into '$TARGET'"

if [[ -n "$MEDIA" ]]; then
  mkdir -p "$MEDIA_ROOT"
  tar -xzf "$MEDIA" -C "$MEDIA_ROOT"
  echo "media restored into $MEDIA_ROOT"
fi
