"""Test utilities for job lifecycle driving."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Job, TranscriptWord, Video
from app.schemas import JobStatus

logger = get_logger(__name__)


# Deterministic fake transcript for testing
FAKE_TRANSCRIPT_WORDS = [
    {"word_index": 0, "word": "hello", "start_time": 0.0, "end_time": 0.5},
    {"word_index": 1, "word": "world", "start_time": 0.5, "end_time": 1.0},
    {"word_index": 2, "word": "this", "start_time": 1.0, "end_time": 1.3},
    {"word_index": 3, "word": "is", "start_time": 1.3, "end_time": 1.5},
    {"word_index": 4, "word": "a", "start_time": 1.5, "end_time": 1.6},
    {"word_index": 5, "word": "test", "start_time": 1.6, "end_time": 2.0},
    {"word_index": 6, "word": "hello", "start_time": 2.0, "end_time": 2.5},
    {"word_index": 7, "word": "again", "start_time": 2.5, "end_time": 3.0},
    {"word_index": 8, "word": "world", "start_time": 3.0, "end_time": 3.5},
    {"word_index": 9, "word": "test", "start_time": 3.5, "end_time": 4.0},
]

FAKE_TRANSCRIPT_TEXT = "hello world this is a test hello again world test"
FAKE_TRANSCRIPT_TSVECTOR = "'hello':1,7 'world':2,9 'this':3 'is':4 'a':5 'test':6,10 'again':8"


class JobDriver:
    """Test-only utility to drive job lifecycle transitions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def advance_to_processing(self, job_id: UUID) -> Job:
        """Advance job to processing status."""
        job = await self.session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        job.status = JobStatus.PROCESSING
        job.progress = 10
        await self.session.flush()
        logger.info("Job advanced to processing", job_id=job_id)
        return job

    async def complete_with_fake_transcript(self, job_id: UUID) -> Job:
        """
        Complete job with deterministic fake transcript data.
        Updates both Job and Video with transcript.
        """
        job = await self.session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        video = await self.session.get(Video, job.video_id)
        if not video:
            raise ValueError(f"Video {job.video_id} not found")

        # Create transcript words
        for word_data in FAKE_TRANSCRIPT_WORDS:
            word = TranscriptWord(
                video_id=video.id,
                **word_data,
            )
            self.session.add(word)

        # Update video
        video.transcript = FAKE_TRANSCRIPT_TEXT
        video.transcript_tsvector = FAKE_TRANSCRIPT_TSVECTOR
        video.language = "en"
        from sqlalchemy import func

        video.transcribed_at = func.now()

        # Update job
        job.status = JobStatus.COMPLETED
        job.progress = 100

        await self.session.flush()
        logger.info("Job completed with fake transcript", job_id=job_id, video_id=video.id)
        return job

    async def fail_job(
        self, job_id: UUID, error: str = "Transcription failed after 3 retries"
    ) -> Job:
        """Mark job as failed with user-safe error."""
        job = await self.session.get(Job, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        job.status = JobStatus.FAILED
        job.error = error
        await self.session.flush()
        logger.info("Job failed", job_id=job_id, error=error)
        return job
