"""Repository layer for database operations."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Job, TranscriptWord, Video

logger = get_logger(__name__)


class VideoRepository:
    """Repository for Video operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, video_id: UUID) -> Video | None:
        """Get video by primary key."""
        result = await self.session.execute(select(Video).where(Video.id == video_id))
        return result.scalar_one_or_none()

    async def get_by_video_id(self, video_id: str) -> Video | None:
        """Get video by YouTube video ID."""
        result = await self.session.execute(select(Video).where(Video.video_id == video_id))
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        youtube_url: str,
        video_id: str,
        title: str | None = None,
        duration_seconds: int | None = None,
    ) -> Video:
        """Create a new video record."""
        video = Video(
            youtube_url=youtube_url,
            video_id=video_id,
            title=title,
            duration_seconds=duration_seconds,
        )
        self.session.add(video)
        await self.session.flush()
        logger.info("Created video", video_id=video.id, youtube_video_id=video_id)
        return video

    async def update_metadata(
        self,
        video_id: UUID,
        *,
        title: str,
        duration_seconds: int,
    ) -> Video:
        """Update video metadata from the media provider."""
        video = await self.get_by_id(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video.title = title
        video.duration_seconds = duration_seconds
        await self.session.flush()
        logger.info("Updated video metadata", video_id=video_id)
        return video

    async def update_transcript(
        self, video_id: UUID, *, transcript: str, language: str | None = None
    ) -> Video:
        """Store transcript text, language and full-text vector for a video."""
        video = await self.get_by_id(video_id)
        if not video:
            raise ValueError(f"Video {video_id} not found")
        video.transcript = transcript
        video.language = language
        video.transcript_tsvector = func.to_tsvector("english", transcript)
        video.transcribed_at = func.now()
        await self.session.flush()
        logger.info("Updated video transcript", video_id=video_id)
        return video

    async def is_transcribed(self, video_id: UUID) -> bool:
        """Check if video has been transcribed."""
        result = await self.session.execute(
            select(Video.transcribed_at).where(Video.id == video_id)
        )
        transcribed_at = result.scalar_one_or_none()
        return transcribed_at is not None


class TranscriptWordRepository:
    """Repository for TranscriptWord operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def bulk_create(self, words: list[TranscriptWord]) -> None:
        """Bulk create transcript words."""
        self.session.add_all(words)
        await self.session.flush()
        logger.info("Bulk created transcript words", count=len(words))

    async def get_by_video_id(self, video_id: UUID) -> Sequence[TranscriptWord]:
        """Get all transcript words for a video ordered by word_index."""
        result = await self.session.execute(
            select(TranscriptWord)
            .where(TranscriptWord.video_id == video_id)
            .order_by(TranscriptWord.word_index)
        )
        return result.scalars().all()

    async def search_exact_phrase(
        self,
        video_id: UUID,
        normalized_words: list[str],
    ) -> list[TranscriptWord]:
        """Return candidate words matching any phrase term (see SearchService)."""
        if not normalized_words:
            return []
        result = await self.session.execute(
            select(TranscriptWord)
            .where(TranscriptWord.video_id == video_id)
            .where(TranscriptWord.word.in_(normalized_words))
            .order_by(TranscriptWord.word_index)
        )
        return list(result.scalars().all())


class JobRepository:
    """Repository for Job operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, job_id: UUID) -> Job | None:
        """Get job by primary key."""
        result = await self.session.execute(select(Job).where(Job.id == job_id))
        return result.scalar_one_or_none()

    async def get_in_flight_by_video_id(self, video_id: UUID) -> Job | None:
        """Get in-flight job (pending or processing) for a video."""
        result = await self.session.execute(
            select(Job)
            .where(Job.video_id == video_id)
            .where(Job.status.in_(["pending", "processing"]))
        )
        return result.scalar_one_or_none()

    async def create_or_get_in_flight(self, video_id: UUID) -> Job:
        """
        Return the in-flight job for a video or create a new one.

        Safe against concurrent requests: the partial unique index on
        in-flight jobs guarantees a single active job per video, so an
        IntegrityError means another worker won the race.
        """
        existing = await self.get_in_flight_by_video_id(video_id)
        if existing:
            logger.info("Found existing in-flight job", job_id=existing.id, video_id=video_id)
            return existing

        job = Job(video_id=video_id, status="pending")
        self.session.add(job)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            winner = await self.get_in_flight_by_video_id(video_id)
            if winner:
                logger.info(
                    "Reused job created by concurrent request",
                    job_id=winner.id,
                    video_id=video_id,
                )
                return winner
            logger.error(
                "Failed to create in-flight job after race", video_id=video_id, error=str(exc)
            )
            raise

        logger.info("Created new job", job_id=job.id, video_id=video_id)
        return job

    async def update_status(
        self,
        job_id: UUID,
        *,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ) -> Job:
        """Update job status."""
        job = await self.get_by_id(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        job.status = status
        if progress is not None:
            job.progress = progress
        if error is not None:
            job.error = error
        await self.session.flush()
        logger.info("Updated job status", job_id=job_id, status=status)
        return job
