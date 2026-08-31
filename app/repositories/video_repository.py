
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import  Video

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
        language: str,
        title: str | None = None,
        duration_seconds: int | None = None,
    ) -> Video:
        """Create a new video record."""
        video = Video(
            youtube_url=youtube_url,
            video_id=video_id,
            title=title,
            duration_seconds=duration_seconds,
            language=language,
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
