"""Messaging client for publishing transcription jobs to the worker queue."""

import ssl
from uuid import UUID

from celery import Celery

from app.core.config import get_settings

_WORKER_TASK_NAME = "app.tasks.transcription.download_and_transcribe"

_settings = get_settings()

_redis_url = _settings.redis_url
if _redis_url.startswith("rediss://"):
    separator = "&" if "?" in _redis_url else "?"
    _redis_url = f"{_redis_url}{separator}ssl_cert_reqs=CERT_REQUIRED"

celery_app = Celery(
    "jumpto",
    broker=_redis_url,
    backend=_redis_url,
)

_redis_url = _settings.redis_url

if _redis_url.startswith("rediss://"):
    separator = "&" if "?" in _redis_url else "?"
    _redis_url = f"{_redis_url}{separator}ssl_cert_reqs=CERT_REQUIRED"

celery_app = Celery(
    "jumpto",
    broker=_redis_url,
    backend=_redis_url,
)

if _redis_url.startswith("rediss://"):
    celery_app.conf.broker_use_ssl = {
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }

    celery_app.conf.result_backend_transport_options = {
        "ssl_cert_reqs": ssl.CERT_REQUIRED,
    }

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
