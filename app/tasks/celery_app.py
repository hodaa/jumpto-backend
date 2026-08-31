"""Celery application configuration."""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "jumpto",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.transcription"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=1800,  # 30 minutes
    task_soft_time_limit=1500,  # 25 minutes
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)
