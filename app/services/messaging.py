"""Messaging client for publishing transcription jobs to the worker queue."""

from uuid import UUID

from celery import Celery

from app.core.config import get_settings

_WORKER_TASK_NAME = "app.tasks.transcription.download_and_transcribe"

_settings = get_settings()

celery_app = Celery(
    "jumpto",
    broker=_settings.redis_url,
    backend=_settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


def dispatch_transcription(job_id: UUID) -> None:
    """Publish a transcription job to the worker's queue."""
    celery_app.send_task(_WORKER_TASK_NAME, args=[str(job_id)])
