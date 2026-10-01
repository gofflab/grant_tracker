#!/bin/sh
# Wait for PostgreSQL, apply migrations, create the first owner if configured, then start.
set -e

python - <<'PY'
import os, sys, time
import psycopg
for attempt in range(60):
    try:
        psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "db"), port=os.environ.get("POSTGRES_PORT", "5432"),
            dbname=os.environ.get("POSTGRES_DB", "grant_tracker"), user=os.environ.get("POSTGRES_USER", "grant_tracker"),
            password=os.environ.get("POSTGRES_PASSWORD", ""), connect_timeout=3,
        ).close()
        break
    except Exception as exc:
        print(f"Waiting for database ({exc.__class__.__name__})...", flush=True)
        time.sleep(2)
else:
    sys.exit("Database never became available.")
PY

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
    python manage.py migrate --noinput
    python manage.py ensure_owner
fi

exec "$@"
