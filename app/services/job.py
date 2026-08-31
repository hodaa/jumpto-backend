"""Job service for managing transcription jobs."""

from uuid import UUID

from app.core.exceptions import JobNotFoundError, VideoNotFoundError
from app.core.logging import get_logger
from app.models import Job
from app.repositories import JobRepository, VideoRepository
from app.schemas import JobStatus

logger = get_logger(__name__)


class JobService:
    """Service for job lifecycle management."""

    def __init__(
        self,
        job_repo: JobRepository,
        video_repo: VideoRepository,
    ) -> None:
        self.job_repo = job_repo
        self.video_repo = video_repo

    async def create_or_get_job(self, video_id: UUID) -> Job:
        """
        Create a new job or get existing in-flight job for a video.

        Args:
            video_id: Video UUID

        Returns:
            Job instance (new or existing in-flight)
        """
        # Verify video exists
        video = await self.video_repo.get_by_id(video_id)
        if not video:
            raise VideoNotFoundError(str(video_id))

        return await self.job_repo.create_or_get_in_flight(video_id)

    async def get_job(self, job_id: UUID) -> Job:
        """
        Get job by ID.

        Args:
            job_id: Job UUID

        Returns:
            Job instance

        Raises:
            JobNotFoundError: If job not found
        """
        job = await self.job_repo.get_by_id(job_id)
        if not job:
            raise JobNotFoundError(str(job_id))
        return job

    async def advance_to_processing(self, job_id: UUID) -> Job:
        """
        Advance job to processing status.

        Args:
            job_id: Job UUID

        Returns:
            Updated job
        """
        return await self.job_repo.update_status(job_id, status=JobStatus.PROCESSING, progress=10)

    async def complete_job(self, job_id: UUID) -> Job:
        """
        Mark job as completed.

        Args:
            job_id: Job UUID

        Returns:
            Updated job
        """
        return await self.job_repo.update_status(
            job_id,
            status=JobStatus.COMPLETED,
            progress=100,
        )

    async def fail_job(self, job_id: UUID, error: str) -> Job:
        """
        Mark job as failed.

        Args:
            job_id: Job UUID
            error: User-safe error message

        Returns:
            Updated job
        """
        return await self.job_repo.update_status(
            job_id,
            status=JobStatus.FAILED,
            error=error,
        )
