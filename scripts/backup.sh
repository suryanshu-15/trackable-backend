#!/usr/bin/env bash
# Back up the PostgreSQL database (custom format) and uploaded media, with checksums and retention.
# Usage: scripts/backup.sh            env: BACKUP_DIR (default ./backups), RETENTION_DAYS (default 14)
set -euo pipefail
source "$(dirname "$0")/_env.sh"
BACKUP_DIR="${BACKUP_DIR:-$ROOT/backups}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

DUMP="$BACKUP_DIR/trackable-db-$STAMP.dump"
pg_dump --format=custom --compress=6 --no-owner --file="$DUMP" "$DB_NAME"
pg_restore --list "$DUMP" > /dev/null          # verify the archive is readable
echo "database: $DUMP ($(du -h "$DUMP" | cut -f1))"

if [[ -d "$MEDIA_ROOT" ]]; then
  MEDIA="$BACKUP_DIR/trackable-media-$STAMP.tar.gz"
  tar -czf "$MEDIA" -C "$MEDIA_ROOT" .
  echo "media:    $MEDIA ($(du -h "$MEDIA" | cut -f1))"
fi

( cd "$BACKUP_DIR" && sha256sum trackable-*-"$STAMP".* > "trackable-$STAMP.sha256" )
find "$BACKUP_DIR" -name 'trackable-*' -type f -mtime +"$RETENTION_DAYS" -print -delete | sed 's/^/pruned:   /'
echo "OK $STAMP"
