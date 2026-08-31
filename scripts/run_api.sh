#!/usr/bin/env bash
set -euo pipefail

# Run FastAPI development server

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

cd "$BACKEND_DIR"

echo "Starting FastAPI server on http://localhost:8000"
echo "Backend directory: $BACKEND_DIR"

# Check if virtual environment exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -e ".[dev]"
fi

# Activate venv and run
source .venv/bin/activate
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload