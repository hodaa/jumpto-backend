# JumpTo Celery worker image
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for yt-dlp (network/SSL) and asyncpg
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first to leverage layer caching
COPY pyproject.toml ./
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN pip install --no-cache-dir .

# Run DB migrations, then start the Celery worker
CMD ["sh", "-c", "alembic upgrade head && celery -A app.tasks.celery_app worker --loglevel=info"]
