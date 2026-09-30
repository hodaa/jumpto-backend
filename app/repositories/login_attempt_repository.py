"""A rolling per-IP failed-login ceiling.

The per-account lockout stops one address from being ground down three times
over. This stops one host from doing that to many accounts in a row. The count
lives in Postgres rather than memory so it survives a restart and works across
multiple API instances.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import LoginAttempt

logger = get_logger(__name__)

WINDOW = timedelta(hours=1)


def window_start(now: datetime | None = None) -> datetime:
    """Floor the clock to the current hour so one address has one row per window."""
    current = now or datetime.now(UTC)
    return current.replace(minute=0, second=0, microsecond=0)


class LoginAttemptRepository:
    """Counts failed logins per address inside a fixed hourly window."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def is_throttled(self, *, ip: str, limit: int) -> bool:
        """Whether this address has already used up its hourly allowance."""
        result = await self.session.execute(
            select(LoginAttempt.failed_count).where(
                LoginAttempt.ip == ip,
                LoginAttempt.window_started_at == window_start(),
            )
        )
        count = result.scalar_one_or_none()
        return (count or 0) >= limit

    async def record_failure(self, *, ip: str) -> int:
        """Increment the window's counter and return its new value."""
        statement = (
            pg_insert(LoginAttempt)
            .values(
                ip=ip,
                window_started_at=window_start(),
                failed_count=1,
            )
            .on_conflict_do_update(
                index_elements=[LoginAttempt.ip, LoginAttempt.window_started_at],
                set_={"failed_count": LoginAttempt.failed_count + 1},
            )
            .returning(LoginAttempt.failed_count)
        )
        result = await self.session.execute(statement)
        count = result.scalar_one()
        logger.info("Recorded failed login", ip=ip, window_count=count)
        return count

    async def clear(self, *, ip: str) -> None:
        """Drop the window after a successful login from that address."""
        await self.session.execute(
            delete(LoginAttempt).where(
                LoginAttempt.ip == ip,
                LoginAttempt.window_started_at == window_start(),
            )
        )

    async def purge_old_windows(self, *, older_than: timedelta = WINDOW) -> int:
        """Delete counters from previous hours so the table stays small."""
        result = await self.session.execute(
            delete(LoginAttempt).where(LoginAttempt.window_started_at < window_start() - older_than)
        )
        return result.rowcount or 0
