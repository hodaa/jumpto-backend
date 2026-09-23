"""Webhook handling for provider completion callbacks."""

from uuid import UUID

from app.core.logging import get_logger
from app.schemas import AssemblyWebhookStatus, JobStatus
from app.services.job import JobService
from app.services.messaging import dispatch_resume_transcription

logger = get_logger(__name__)

# User-safe message used when Assembly.ai reports an error for a job.
_WEBHOOK_ERROR_MESSAGE = "Audio transcription failed in Assembly.ai. Please try again."
_IN_FLIGHT_STATUSES = (JobStatus.PENDING.value, JobStatus.PROCESSING.value)


async def handle_assembly_webhook(
    job_service: JobService,
    *,
    job_id: UUID,
    transcript_id: str,
    status: AssemblyWebhookStatus,
    provider: str,
) -> None:
    """Route an Assembly.ai completion callback to the right job outcome.

    A completed transcript re-enqueues the worker task with the Assembly
    transcript id as its resume token so the worker polls instead of
    re-downloading the audio. An error status fails the job. Terminal jobs are
    left untouched (idempotent: Assembly retries non-2xx deliveries).
    """
    job = await job_service.get_job(job_id)
    if job.status not in _IN_FLIGHT_STATUSES:
        logger.info("Ignoring webhook for terminal job", job_id=job_id, status=job.status)
        return

    if status == AssemblyWebhookStatus.COMPLETED:
        dispatch_resume_transcription(job_id, transcript_id, provider)
        logger.info(
            "Re-enqueued job from Assembly webhook",
            job_id=job_id,
            transcript_id=transcript_id,
            provider=provider,
        )
        return

    await job_service.fail_job(job_id, _WEBHOOK_ERROR_MESSAGE)
    logger.info("Failed job from Assembly webhook", job_id=job_id, transcript_id=transcript_id)
