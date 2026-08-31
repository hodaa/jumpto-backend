# JumpTo — YouTube Keyword Timestamp Finder

A web application where users input a YouTube URL and a keyword/phrase to get exact timestamps where that phrase is spoken.

## Architecture

```
React (frontend) → FastAPI (backend) → PostgreSQL + Redis + Assembly.ai
```

## Quick Start

### Prerequisites

- Python 3.12+
- Node.js 20+
- PostgreSQL 17+ (running on localhost:5432)
- Redis 7+ (running on localhost:6379)

### Backend Setup

```bash
cd backend

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Copy environment template
cp ../.env.example .env
# Edit .env with your Assembly.ai API key

# Setup databases
../scripts/setup_db.sh

# Run API server
../scripts/run_api.sh
```

API will be available at `http://localhost:8000`

### Worker (transcription pipeline)

```bash
# From repo root, in another terminal
scripts/run_worker.sh
```

### Frontend Setup

```bash
cd frontend
npm install
cp .env.example .env        # VITE_API_BASE_URL defaults to http://localhost:8000
npm run dev
```

Frontend will be available at `http://localhost:5173` (defaults to Arabic, RTL; toggle to English in the header). In dev, `/api/*` requests are proxied to the backend on port 8000.

## API Endpoints

### POST /api/search
Search for keyword in YouTube video.

**Request:**
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "keyword": "never gonna give you up"
}
```

**Response (cached):**
```json
{
  "status": "found",
  "results": [
    {"timestamp": "00:00", "progress_seconds": 0.0, "text_snippet": "never gonna give you up"},
    {"timestamp": "00:02", "progress_seconds": 2.0, "text_snippet": "never gonna give you up"}
  ]
}
```

**Response (not cached):**
```json
{
  "status": "processing",
  "job_id": "uuid-here"
}
```

### GET /api/status/{job_id}
Check transcription job status.

**Response:**
```json
{
  "status": "pending|processing|completed|failed",
  "progress": 50,
  "results": [...],
  "error": "user-safe error message if failed"
}
```

### GET /api/video/{video_id}/search?keyword=...
Search cached transcript for specific video.

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| DATABASE_URL | PostgreSQL connection URL | `postgresql://postgres:postgres@localhost:5432/jumpto` |
| ASSEMBLY_API_KEY | Assembly.ai API key | **required** |
| REDIS_URL | Redis connection URL | `redis://localhost:6379/0` |
| CORS_ORIGINS | Allowed CORS origins (comma-separated) | `http://localhost:5173` |
| ENVIRONMENT | `development` or `production` | `development` |
| JUMPTO_LIVE_EXTERNAL_CALLS | Enable live yt-dlp/Assembly.ai | `false` |
| JUMPTO_TRANSCRIPT_MODE | `real` or `fake` | `real` |

Set `JUMPTO_TRANSCRIPT_MODE=fake` to run without an Assembly.ai key (downloads are skipped and a fake transcript is used in dev).

## Testing

### Backend

```bash
cd backend
source .venv/bin/activate
pytest --cov=app --cov-fail-under=80 -v
```

### Frontend

```bash
cd frontend
npx vitest run --coverage   # 80% thresholds enforced in vite.config.ts
npm run lint                # ESLint strict
npx prettier --check .      # formatting
npm run build               # tsc --noEmit && vite build
```

## Project Structure

```
jumpto/
├── backend/           # FastAPI application
│   ├── app/
│   │   ├── api/       # API routes
│   │   ├── core/      # Config, DB, logging, exceptions
│   │   ├── models/    # SQLAlchemy models
│   │   ├── repositories/  # Data access layer
│   │   ├── services/  # Business logic
│   │   ├── schemas/   # Pydantic schemas
│   │   └── tasks/     # Celery tasks
│   ├── tests/
│   │   ├── unit/      # Unit tests
│   │   ├── integration/ # Integration tests
│   │   └── utils/     # Test utilities
│   ├── alembic/       # Database migrations
│   └── pyproject.toml
├── frontend/          # React application (TypeScript, Vite, Vitest, RTL)
│   ├── src/
│   │   ├── api/       # HTTP client + error mapping
│   │   ├── components/# UI components (bilingual EN/AR, RTL)
│   │   ├── hooks/     # Job polling hook
│   │   ├── i18n/      # react-i18next catalogs (en, ar)
│   │   └── utils/     # YouTube URL parsing
│   └── vite.config.ts
├── scripts/           # Setup and run scripts
├── .env.example       # Environment template
└── .gitignore
```

## Development

### Database Migrations

```bash
cd backend
# Create new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head
```

### Code Quality

```bash
# Format
black backend/

# Lint
ruff check backend/

# Type check (if using mypy)
mypy backend/app/
```

## License

MIT