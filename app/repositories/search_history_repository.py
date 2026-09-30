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
from app.models import SearchHistory

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
    ) -> SearchHistory:
        """Record one search for a user."""
        entry = SearchHistory(
            user_id=user_id,
            video_id=video_id,
            keyword=keyword,
            status=status,
            locale=locale,
            source=source,
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
        return list(result.scalars().all())

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
