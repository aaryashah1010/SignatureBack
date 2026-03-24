#!/bin/sh
set -e

export PYTHONPATH="/app:${PYTHONPATH}"

DB_HOST="${DB_HOST:-postgres}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-postgres}"

echo "Waiting for PostgreSQL at ${DB_HOST}:${DB_PORT}..."
until pg_isready -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" >/dev/null 2>&1; do
  sleep 1
done

alembic upgrade head
LOG_LEVEL="${LOG_LEVEL:-warning}"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level "$LOG_LEVEL" --workers 1
