#!/usr/bin/env bash
set -euo pipefail

# Setup databases for JumpTo

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKEND_DIR="$PROJECT_ROOT/backend"

echo "Setting up JumpTo databases..."

# Create databases
echo "Creating databases..."
psql -h localhost -U postgres -c "CREATE DATABASE jumpto;" 2>/dev/null || echo "Database 'jumpto' already exists"
psql -h localhost -U postgres -c "CREATE DATABASE jumpto_test;" 2>/dev/null || echo "Database 'jumpto_test' already exists"

# Run migrations on main database
echo "Running migrations on jumpto..."
cd "$BACKEND_DIR"
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/jumpto" alembic upgrade head

# Run migrations on test database
echo "Running migrations on jumpto_test..."
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/jumpto_test" alembic upgrade head

echo "Database setup complete!"