"""Session and login-attempt persistence.

All SQL lives here; the service layer above depends on this repository, and
nothing here knows about HTTP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import Session, User

logger = get_logger(__name__)

TOKEN_HASH_LENGTH = 64


class SessionRepository:
    """Repository for Session operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        token_hash: str,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> Session:
        """Persist a new session row.

        The idle window is not a column: it is derived from ``last_used_at`` on
        read, so only the absolute deadline needs storing.
        """
        record = Session(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            last_used_at=datetime.now(UTC),
            ip=ip,
            user_agent=(user_agent or "")[:512] or None,
        )
        self.session.add(record)
        await self.session.flush()
        logger.info("Created session", user_id=str(user_id), session_id=str(record.id))
        return record

    async def get_valid(self, token_hash: str, *, idle_ttl: timedelta) -> Session | None:
        """Return an unexpired, unrevoked session for a token hash.

        Enforces both deadlines: the absolute ``expires_at`` and the idle
        window derived from ``last_used_at``.
        """
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(Session).where(
                Session.token_hash == token_hash,
                Session.revoked_at.is_(None),
                Session.expires_at > now,
                Session.last_used_at > now - idle_ttl,
            )
        )
        return result.scalar_one_or_none()

    async def touch(self, session_id: uuid.UUID) -> None:
        """Record activity so the idle window slides forward.

        Only pushes ``last_used_at``; ``expires_at`` stays put so a session can
        never outlive its absolute cap by sliding.
        """
        await self.session.execute(
            update(Session).where(Session.id == session_id).values(last_used_at=datetime.now(UTC))
        )

    async def revoke(self, session_id: uuid.UUID) -> None:
        """Revoke a single session."""
        await self.session.execute(
            update(Session)
            .where(Session.id == session_id, Session.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        """Revoke every live session for a user (password reset, admin unlock)."""
        await self.session.execute(
            update(Session)
            .where(Session.user_id == user_id, Session.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def sweep_expired(self, *, batch_limit: int = 1000) -> int:
        """Delete expired or revoked rows.

        Redis TTLs would have done this for free; a table does not, so without
        this the index degrades as sessions accumulate.
        """
        now = datetime.now(UTC)
        # LIMIT goes in the where clause: SQLAlchemy's bulk delete has no
        # .limit(), and a DELETE ... LIMIT needs a CTE on Postgres.
        stale = (
            select(Session.id)
            .where((Session.expires_at <= now) | (Session.revoked_at.is_not(None)))
            .limit(batch_limit)
        )
        result = await self.session.execute(
            delete(Session).where(Session.id.in_(stale.scalar_subquery()))
        )
        return result.rowcount or 0


class UserRepository:
    """Repository for User operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_email(self, email: str) -> User | None:
        """Return a user by normalized (lowercase) email."""
        result = await self.session.execute(select(User).where(User.email == email.lower()))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Return a user by primary key."""
        result = await self.session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def get_by_google_sub(self, google_sub: str) -> User | None:
        """Return a user by their Google subject identifier."""
        result = await self.session.execute(select(User).where(User.google_sub == google_sub))
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        email: str,
        password_hash: str | None,
        google_sub: str | None = None,
    ) -> User:
        """Create a user row."""
        user = User(
            email=email.lower(),
            password_hash=password_hash,
            google_sub=google_sub,
        )
        self.session.add(user)
        await self.session.flush()
        logger.info("Created user", user_id=str(user.id), email=user.email)
        return user

    async def link_google_sub(self, user_id: uuid.UUID, google_sub: str) -> None:
        """Attach a Google identity to an existing user."""
        await self.session.execute(
            update(User).where(User.id == user_id).values(google_sub=google_sub)
        )

    async def mark_email_verified(self, user_id: uuid.UUID) -> None:
        """Stamp the email as verified."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id, User.email_verified_at.is_(None))
            .values(email_verified_at=datetime.now(UTC))
        )

    async def record_failed_login(
        self,
        user_id: uuid.UUID,
        *,
        max_attempts: int,
    ) -> int:
        """Increment the failure counter and lock the account at the threshold.

        Returns the new consecutive-failure count.
        """
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if user is None:
            return 0
        user.failed_login_count = (user.failed_login_count or 0) + 1
        user.last_failed_login_at = now
        if user.failed_login_count >= max_attempts and user.locked_at is None:
            user.locked_at = now
            logger.warning("Account locked after failed logins", user_id=str(user_id))
        await self.session.flush()
        return user.failed_login_count

    async def clear_failures(self, user_id: uuid.UUID) -> None:
        """Reset the failure counter and clear any lock after a success."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(failed_login_count=0, locked_at=None, last_failed_login_at=None)
        )

    async def update_password(self, user_id: uuid.UUID, password_hash: str) -> None:
        """Set a new password hash and clear lockout state."""
        await self.session.execute(
            update(User)
            .where(User.id == user_id)
            .values(
                password_hash=password_hash,
                failed_login_count=0,
                locked_at=None,
                last_failed_login_at=None,
            )
        )

    async def unlock(self, email: str) -> bool:
        """Clear lockout state for an address. Used by the admin CLI.

        Returns False when no such user exists.
        """
        result = await self.session.execute(
            update(User)
            .where(User.email == email.lower(), User.locked_at.is_not(None))
            .values(failed_login_count=0, locked_at=None, last_failed_login_at=None)
        )
        await self.session.flush()
        return bool(result.rowcount)


def absolute_deadline(*, absolute_ttl: timedelta) -> datetime:
    """Return the hard expiry for a new session.

    The idle window is not stored: it is computed from ``last_used_at`` when the
    session is read, so it slides on activity while this cap does not.
    """
    return datetime.now(UTC) + absolute_ttl
