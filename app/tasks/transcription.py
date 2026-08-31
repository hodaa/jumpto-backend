"""Transcription pipeline task."""

import asyncio
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import async_session_factory
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.models import TranscriptWord
from app.providers import TranscriptData, get_media_info, get_transcript_provider
from app.repositories import JobRepository, TranscriptWordRepository, VideoRepository
from app.schemas import JobStatus
from app.services.job import JobService
from app.services.search import SearchService
from app.tasks.celery_app import celery_app

logger = get_logger(__name__)

_RETRY_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 2
_USER_SAFE_FAILURE = "Transcription failed. Please try again later."
_EXTERNAL_FAILURE = "Could not fetch the transcript for this video. Please try again later."


async def run_pipeline(
    job_id: str,
    video_id: str,
    youtube_url: str,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """Run the transcription pipeline for a job in a dedicated DB session."""
    session_factory = session_factory or async_session_factory
    async with session_factory() as session:
        job_service = JobService(JobRepository(session), VideoRepository(session))
        video_repo = VideoRepository(session)
        word_repo = TranscriptWordRepository(session)
        job = await job_service.get_job(UUID(job_id))
        if job.status != JobStatus.PENDING:
            logger.info("Skipping non-pending job", job_id=job_id, status=job.status)
            return {"status": job.status}

        try:
            await job_service.advance_to_processing(UUID(job_id))
            media = get_media_info(video_id, youtube_url)
            await video_repo.update_metadata(
                job.video_id,
                title=media.title,
                duration_seconds=media.duration_seconds,
            )
            transcript = await _fetch_transcript_with_retry(youtube_url)
            await _store_transcript(word_repo, video_repo, job.video_id, transcript)
            await job_service.complete_job(UUID(job_id))
            await session.commit()
        except Exception as exc:
            await session.rollback()
            error = _user_safe_message(exc)
            logger.exception("Transcription pipeline failed", job_id=job_id, error=error)
            await job_service.fail_job(UUID(job_id), error)
            await session.commit()
            raise

    logger.info("Pipeline completed", job_id=job_id)
    return {"status": "completed", "video_id": video_id}


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def download_and_transcribe(self, job_id: str, video_id: str, youtube_url: str) -> dict:
    """Run the transcription pipeline from the Celery worker."""
    asyncio.run(run_pipeline(job_id, video_id, youtube_url))
    return {"status": "completed", "video_id": video_id}


async def _store_transcript(
    word_repo: TranscriptWordRepository,
    video_repo: VideoRepository,
    video_id: UUID,
    transcript: TranscriptData,
) -> None:
    """Normalize and persist transcript words and full text."""
    words = [
        TranscriptWord(
            video_id=video_id,
            word_index=index,
            word=SearchService.normalize_word(word.word),
            start_time=word.start_time,
            end_time=word.end_time,
        )
        for index, word in enumerate(transcript.words)
    ]
    if words:
        await word_repo.bulk_create(words)
    await video_repo.update_transcript(
        video_id,
        transcript=transcript.text,
        language=transcript.language,
    )


async def _fetch_transcript_with_retry(youtube_url: str) -> TranscriptData:
    """Fetch a transcript, retrying transient external failures."""
    provider = get_transcript_provider()
    last_error: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await provider.fetch(youtube_url)
        except ExternalServiceError as exc:
            last_error = exc
            logger.warning("Transcript fetch attempt failed", attempt=attempt + 1)
            if attempt + 1 < _RETRY_ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
    if last_error:
        raise last_error
    return await provider.fetch(youtube_url)  # pragma: no cover


def _user_safe_message(exc: Exception) -> str:
    """Map an exception to a user-safe failure message."""
    if isinstance(exc, ExternalServiceError):
        return _EXTERNAL_FAILURE
    return _USER_SAFE_FAILURE
