"""Shared identity lookup for the session token.

Both the auth routes and the public search route need to turn a raw token into
a user. Keeping the construction in one place means the search route cannot
accidentally resolve a session differently from the auth routes.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.search_history_repository import SearchHistoryRepository
from app.repositories.session_repository import SessionRepository, UserRepository
from app.services.auth import AuthIdentity, AuthService
from app.services.email import GmailSmtpEmailSender


def build_auth_service(*, db: AsyncSession, settings: Settings) -> AuthService:
    """Assemble the auth service over the request-scoped session."""
    return AuthService(
        user_repo=UserRepository(db),
        session_repo=SessionRepository(db),
        token_repo=AuthTokenRepository(db),
        history_repo=SearchHistoryRepository(db),
        attempt_repo=LoginAttemptRepository(db),
        db=db,
        settings=settings,
        email_sender=GmailSmtpEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            user=settings.smtp_user,
            app_password=settings.smtp_app_password,
            from_email=settings.email_from or settings.smtp_user,
            from_name=settings.email_from_name,
        ),
    )


async def resolve_identity(
    *, db: AsyncSession, settings: Settings, token: str
) -> AuthIdentity | None:
    """Return the identity for a live session token, else None."""
    return await build_auth_service(db=db, settings=settings).resolve_session(token)
