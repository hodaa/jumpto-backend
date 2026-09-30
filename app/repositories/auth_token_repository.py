"""Token generation, hashing, and single-use token storage.

Session and reset tokens are 32 bytes from ``secrets``. Only the SHA-256 hash
is stored, so a database dump or SQL injection yields no live credentials.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models import AuthToken

logger = get_logger(__name__)

TOKEN_BYTES = 32


def generate_token() -> str:
    """Return a URL-safe random token."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest stored for a token."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthTokenRepository:
    """Repository for single-use auth tokens."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        purpose: str,
        ttl: timedelta,
    ) -> str:
        """Issue a token and return the plaintext (only time it is available)."""
        token = generate_token()
        record = AuthToken(
            user_id=user_id,
            token_hash=hash_token(token),
            purpose=purpose,
            expires_at=datetime.now(UTC) + ttl,
        )
        self.session.add(record)
        await self.session.flush()
        logger.info("Issued auth token", user_id=str(user_id), purpose=purpose)
        return token

    async def consume(self, *, token: str, purpose: str) -> uuid.UUID | None:
        """Redeem a token, returning its user id, or None when unusable.

        Single-use: a row that is already used or expired is not redeemable, and
        the redemption stamps ``used_at`` in the same transaction.
        """
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(AuthToken)
            .where(
                AuthToken.token_hash == hash_token(token),
                AuthToken.purpose == purpose,
                AuthToken.used_at.is_(None),
                AuthToken.expires_at > now,
            )
            .with_for_update()
        )
        record = result.scalar_one_or_none()
        if record is None:
            return None
        record.used_at = now
        await self.session.flush()
        return record.user_id

    async def invalidate_all_for_user(self, *, user_id: uuid.UUID, purpose: str) -> None:
        """Void outstanding tokens of a kind, e.g. when a new reset is issued."""
        await self.session.execute(
            delete(AuthToken).where(
                AuthToken.user_id == user_id,
                AuthToken.purpose == purpose,
                AuthToken.used_at.is_(None),
            )
        )

    async def sweep_expired(self) -> int:
        """Delete tokens that expired more than a day ago."""
        cutoff = datetime.now(UTC) - timedelta(days=1)
        result = await self.session.execute(delete(AuthToken).where(AuthToken.expires_at < cutoff))
        return result.rowcount or 0
