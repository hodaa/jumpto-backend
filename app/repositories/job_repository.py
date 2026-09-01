from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Job

logger = get_logger(__name__)


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
        status: str | None = None,
        progress: int | None = None,
        error: str | None = None,
    ) -> Job:
        """Update job status and/or progress/error fields."""
        job = await self.get_by_id(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if error is not None:
            job.error = error
        await self.session.flush()
        logger.info("Updated job status", job_id=job_id, status=job.status)
        return job
