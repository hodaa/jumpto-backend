"""Persistence for per-user search history.

Every read and write takes an explicit ``user_id``. Callers obtain it from the
session, never from the request body - see
``tests/unit/test_history_ownership.py``.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import SearchHistory, Video

logger = get_logger(__name__)

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


def encode_cursor(entry: SearchHistory) -> str:
    """Encode a keyset cursor from a history row."""
    stamp = entry.created_at.astimezone(UTC).isoformat()
    raw = f"{entry.id}:{stamp}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[UUID, datetime] | None:
    """Decode a keyset cursor, returning None when it is malformed."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        id_part, stamp_part = raw.split(":", 1)
        return UUID(id_part), datetime.fromisoformat(stamp_part)
    except (ValueError, TypeError):
        return None


class SearchHistoryRepository:
    """Repository for SearchHistory operations, always scoped to one user."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        user_id: uuid.UUID,
        video_id: str,
        keyword: str,
        status: str,
        locale: str | None = None,
        source: str | None = None,
        progress_seconds: int | None = None,
        video_title: str | None = None,
        match_timestamps: list[int] | None = None,
        match_results: list[dict[str, object]] | None = None,
    ) -> SearchHistory:
        """Record one search for a user."""
        entry = SearchHistory(
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
        self.session.add(entry)
        await self.session.flush()
        logger.info(
            "Recorded search history",
            user_id=str(user_id),
            entry_id=str(entry.id),
            status=status,
        )
        return entry

    async def list_for_user(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> list[SearchHistory]:
        """Return one page of a user's history, newest first.

        Keyset pagination rather than OFFSET: a phone history can grow for
        years, and OFFSET degrades badly at depth.
        """
        page_size = min(max(limit, 1), MAX_PAGE_SIZE)
        query = select(SearchHistory).where(SearchHistory.user_id == user_id)

        if cursor:
            decoded = decode_cursor(cursor)
            if decoded is not None:
                last_id, last_created = decoded
                query = query.where(
                    (SearchHistory.created_at < last_created)
                    | ((SearchHistory.created_at == last_created) & (SearchHistory.id < last_id))
                )

        result = await self.session.execute(
            query.order_by(SearchHistory.created_at.desc(), SearchHistory.id.desc()).limit(
                page_size
            )
        )
        page = list(result.scalars().all())
        await self._fill_missing_titles(page)
        return page

    async def _fill_missing_titles(self, page: list[SearchHistory]) -> None:
        """Backfill titles that were not around to be snapshotted.

        A search of a video we have never seen records its history row the
        moment the transcription is queued - at which point the ``videos`` row
        exists but its ``title`` does not yet, because the title only arrives
        with the transcript. So the first search of any new video is filed
        untitled, and stayed that way even though the title turned up minutes
        later.

        A missing snapshot has nothing to preserve, so fall back to the live
        title for those rows only. Rows that did capture a title keep it: the
        snapshot is deliberate, and the video may since have been renamed.
        """
        missing = {entry.video_id for entry in page if not entry.video_title}
        if not missing:
            return
        titles = dict(
            (
                await self.session.execute(
                    select(Video.video_id, Video.title).where(Video.video_id.in_(missing))
                )
            )
            .tuples()
            .all()
        )
        for entry in page:
            if not entry.video_title:
                # ``.strip()`` so whitespace-only metadata cannot present as a
                # title; the history page treats "" and None alike as untitled.
                entry.video_title = (titles.get(entry.video_id) or "").strip() or None

    async def apply_match_results(
        self, updates: list[tuple[SearchHistory, int, list[dict[str, object]]]]
    ) -> None:
        """Persist lazily-recovered results for history rows.

        The result set is a deterministic function of (video, keyword), so it is
        derived once, here, and then treated as a snapshot like any other: it is
        never recomputed, so a later re-transcription cannot silently rewrite
        what a past search is said to have found.

        The position and the full result list are written together - they come
        from the same search, so storing one without the other would leave a row
        that can seek but not show, or show but not seek.

        One commit for the whole page rather than one per row.
        """
        if not updates:
            return
        for entry, seconds, results in updates:
            # Only fill the position when the caller had none to offer: a value
            # that was already recorded is a deliberate snapshot, and writing a
            # freshly-derived one would move where this entry is said to point.
            if entry.progress_seconds is None:
                entry.progress_seconds = seconds
            entry.match_timestamps = [int(r["progress_seconds"]) for r in results]
            entry.match_results = results
        await self.session.commit()

    async def delete_one(self, *, user_id: uuid.UUID, entry_id: uuid.UUID) -> bool:
        """Delete one entry belonging to the user. Returns False if not theirs."""
        result = await self.session.execute(
            delete(SearchHistory).where(
                SearchHistory.id == entry_id,
                SearchHistory.user_id == user_id,
            )
        )
        return bool(result.rowcount)

    async def delete_all_for_user(self, *, user_id: uuid.UUID) -> int:
        """Clear a user's entire history. Returns the number of rows removed."""
        result = await self.session.execute(
            delete(SearchHistory).where(SearchHistory.user_id == user_id)
        )
        removed = result.rowcount or 0
        logger.info("Cleared search history", user_id=str(user_id), removed=removed)
        return removed

    async def count_for_user(self, *, user_id: uuid.UUID) -> int:
        """Return how many entries a user has."""
        result = await self.session.execute(
            select(SearchHistory.id).where(SearchHistory.user_id == user_id)
        )
        return len(result.scalars().all())
