#!/usr/bin/env bash
# Dumps the production Postgres database to a gzipped file and prunes old
# backups. Run nightly by voicedesk-backup.timer; safe to run by hand too.
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_DIR="${BACKUP_DIR:-/opt/voicedesk/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

cd "$DEPLOY_DIR"
# shellcheck disable=SC1091
set -a; source .env; set +a

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/voicedesk-${stamp}.sql.gz"

echo "Backing up ${POSTGRES_DB:-voicedesk} to ${out}"
docker compose -f docker-compose.prod.yml --env-file .env exec -T postgres \
  pg_dump -U "${POSTGRES_USER:-voicedesk}" "${POSTGRES_DB:-voicedesk}" \
  | gzip > "$out"

# A dump that produced no bytes is worse than no backup at all — it silently
# passes a restore test's "file exists" check while restoring nothing.
if [ ! -s "$out" ]; then
  echo "Backup produced an empty file, removing it and failing." >&2
  rm -f "$out"
  exit 1
fi

echo "Backup complete: $(du -h "$out" | cut -f1)"

find "$BACKUP_DIR" -name 'voicedesk-*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete
