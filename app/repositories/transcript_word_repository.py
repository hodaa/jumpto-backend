from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import TranscriptWord

logger = get_logger(__name__)


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
