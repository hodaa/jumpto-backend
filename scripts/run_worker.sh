#!/usr/bin/env bash
set -euo pipefail

# Run Celery worker (stub for Sprint 01)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

cd "$BACKEND_DIR"

echo "Starting Celery worker..."
echo "Broker: Redis at localhost:6379"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e ".[dev]"
fi

# Activate venv and run
source .venv/bin/activate
exec celery -A app.tasks.celery_app worker --loglevel=info