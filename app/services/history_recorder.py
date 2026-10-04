"""Records a search against the caller's account, if they have one.

This is the seam that keeps the public search endpoint unchanged. The search
route calls ``record`` on every search and never learns whether the visitor is
signed in: anonymous and failed-lookup cases both resolve to a no-op.

Recording must never break a search. A visitor looking for a quote in a video
should never see a 500 because the history write failed, so the write is
isolated in a savepoint and every failure is logged and swallowed.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.repositories.search_history_repository import SearchHistoryRepository

logger = get_logger(__name__)

# Statuses recorded on the history row, mirroring what the search can return.
STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_PROCESSING = "processing"


class SearchHistoryRecorder:
    """Writes one search-history row per signed-in search."""

    def __init__(self, history_repo: SearchHistoryRepository, session: AsyncSession) -> None:
        self.history_repo = history_repo
        self.session = session

    async def record(
        self,
        *,
        user_id: uuid.UUID | None,
        video_id: str,
        keyword: str,
        status: str,
        locale: str | None = None,
        source: str | None = None,
        progress_seconds: int | None = None,
        video_title: str | None = None,
        match_timestamps: list[int] | None = None,
        match_results: list[dict[str, object]] | None = None,
    ) -> None:
        """Record the search, or do nothing at all.

        ``user_id`` is None for anonymous visitors, which is the whole point:
        no row, no account, no change in behaviour.
        """
        if user_id is None:
            return
        try:
            # A savepoint, not the outer transaction: the search route may
            # already have written the video row, and a failure here must not
            # discard that work.
            async with self.session.begin_nested():
                await self.history_repo.record(
                    user_id=user_id,
                    video_id=video_id,
                    keyword=keyword,
                    status=status,
                    locale=locale,
                    source=source,
                    progress_seconds=progress_seconds,
                    video_title=video_title,
                    match_timestamps=match_timestamps,
                    match_results=match_results,
                )
        except SQLAlchemyError:
            # Never fail the visitor's search over a history write.
            logger.warning("Could not record search history", exc_info=True)
