#!/bin/sh
# Choreizo container entrypoint:
#   1. Apply pending Alembic migrations (creates tables on first boot).
#   2. Hand off to the FastAPI app.
set -e

echo "[choreizo] Running Alembic migrations..."
alembic upgrade head

echo "[choreizo] Starting app..."
exec python -m app.main
