#!/usr/bin/env bash
# Start the demo server, freeing port 8000 first so a stale process from a
# previous run can never block startup.
#
# Usage:  ./run.sh          start on port 8000 (default)
#         ./run.sh 8001     start on a different port
set -e

PORT="${1:-8000}"
cd "$(dirname "$0")"

# Free the port if a previous server is still holding it.
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  echo "Port $PORT is in use; stopping the previous server."
  lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

# Prefer the project virtualenv if present.
if [ -x "venv/bin/python" ]; then
  PY="venv/bin/python"
else
  PY="python"
fi

echo "Starting the NRS AEOI-CRS demo on http://localhost:$PORT/"
exec "$PY" manage.py runserver "$PORT"
