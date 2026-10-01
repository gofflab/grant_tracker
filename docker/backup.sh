#!/bin/sh
# Nightly pg_dump of the whole database (records AND uploaded documents) with rotation.
#   docker compose exec backup /backup.sh now     # take a backup immediately
set -eu

: "${BACKUP_HOUR:=2}"
: "${BACKUP_RETENTION_DAYS:=30}"
export PGPASSWORD="$POSTGRES_PASSWORD"

backup() {
    ts=$(date +%Y%m%d-%H%M%S)
    out="/backups/grant_tracker-$ts.dump"
    pg_dump -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --compress=6 -f "$out.partial"
    mv "$out.partial" "$out"
    find /backups -name 'grant_tracker-*.dump' -type f -mtime +"$BACKUP_RETENTION_DAYS" -delete
    echo "$(date -Iseconds) backup written: $out ($(du -h "$out" | cut -f1))"
}

if [ "${1:-}" = "now" ]; then
    backup
    exit 0
fi

echo "Backups daily at ${BACKUP_HOUR}:00, keeping ${BACKUP_RETENTION_DAYS} days, in /backups"
last=""
while true; do
    today=$(date +%Y-%m-%d)
    hour=$(date +%H)
    if [ "$hour" -ge "$BACKUP_HOUR" ] && [ "$last" != "$today" ]; then
        backup || echo "$(date -Iseconds) BACKUP FAILED"
        last=$today
    fi
    sleep 600
done
