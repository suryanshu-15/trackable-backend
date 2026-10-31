#!/usr/bin/env bash
# Backup / restore smoke test (spec test 17): back up, restore into a scratch database,
# compare row counts of every table and check migrations, then drop the scratch database.
set -euo pipefail
source "$(dirname "$0")/_env.sh"
WORK="$(mktemp -d)"
SCRATCH="trackable_restore_check_$(date +%s)"
PY="${PYTHON:-$ROOT/backend/.venv/bin/python}"; [[ -x "$PY" ]] || PY=python
cleanup() { dropdb --if-exists "$SCRATCH" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

BACKUP_DIR="$WORK" RETENTION_DAYS=36500 "$ROOT/scripts/backup.sh" > "$WORK/backup.log"
DUMP="$(ls "$WORK"/trackable-db-*.dump)"
TARGET_DB="$SCRATCH" "$ROOT/scripts/restore.sh" "$DUMP" > /dev/null

counts() {
  psql -d "$1" -Atc "select table_name from information_schema.tables where table_schema='public' and table_type='BASE TABLE' order by 1" |
  while read -r t; do echo "$t $(psql -d "$1" -Atc "select count(*) from \"$t\"")"; done
}
counts "$DB_NAME" > "$WORK/source.txt"
counts "$SCRATCH" > "$WORK/restored.txt"
if ! diff -u "$WORK/source.txt" "$WORK/restored.txt"; then
  echo "FAIL: row counts differ after restore" >&2; exit 1
fi
TABLES=$(wc -l < "$WORK/source.txt"); ROWS=$(awk '{s+=$2} END {print s}' "$WORK/source.txt")

( cd "$ROOT/backend" && DB_NAME="$SCRATCH" "$PY" manage.py migrate --check > /dev/null ) \
  || { echo "FAIL: restored database has unapplied migrations" >&2; exit 1; }
( cd "$ROOT/backend" && DB_NAME="$SCRATCH" "$PY" manage.py reconcile_inventory > /dev/null ) \
  || { echo "FAIL: restored inventory does not reconcile" >&2; exit 1; }
echo "PASS: $TABLES tables / $ROWS rows restored identically; migrations current; inventory reconciles."
