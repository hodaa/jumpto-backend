"""Use cases for registration, login, sessions, and password recovery.

Policy lives here - lockout thresholds, the account-linking rule, and which
error a failure produces. This layer knows nothing about HTTP; the controller
above maps these errors to status codes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import (
    AccountLockedError,
    AuthenticationFailedError,
    AuthServiceUnavailableError,
    EmailAlreadyRegisteredError,
    EmailNotVerifiedError,
    InvalidTokenError,
    TooManyAttemptsError,
)
from app.core.logging import get_logger
from app.models import SearchHistory, User
from app.repositories.auth_token_repository import (
    AuthTokenRepository,
    generate_token,
    hash_token,
)
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.search_history_repository import SearchHistoryRepository
from app.repositories.session_repository import (
    SessionRepository,
    UserRepository,
    absolute_deadline,
)
from app.services.email import GmailSmtpEmailSender
from app.services.google import GoogleTokenVerifier
from app.services.password import hash_password, verify_password
from app.services.ports import AuthEmail

logger = get_logger(__name__)

# A real Argon2id hash of a value nobody knows, used to spend the same
# verification time on an unknown address as on a known one. Without it,
# response latency alone reveals which addresses are registered.
_DUMMY_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$36bfB2Q0KOF2ssAxwRmUBw$"
    "YGCtqpkuKmTo3U+brR7tO+vHVmIo2/4nIcNirrNApeU"
)

PURPOSE_VERIFY = "email_verification"
PURPOSE_RESET = "password_reset"


@dataclass(frozen=True)
class IssuedSession:
    """A freshly created session, with the token available exactly once."""

    user: User
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class AuthIdentity:
    """The signed-in user, resolved from a session token."""

    user: User
    session_id: uuid.UUID


class AuthService:
    """Registration, login, and session lifecycle."""

    def __init__(
        self,
        *,
        user_repo: UserRepository,
        session_repo: SessionRepository,
        token_repo: AuthTokenRepository,
        history_repo: SearchHistoryRepository,
        attempt_repo: LoginAttemptRepository,
        db: AsyncSession,
        settings: Settings,
        email_sender: GmailSmtpEmailSender,
        google_verifier: GoogleTokenVerifier | None = None,
    ) -> None:
        self.user_repo = user_repo
        self.session_repo = session_repo
        self.token_repo = token_repo
        self.history_repo = history_repo
        self.attempt_repo = attempt_repo
        self.db = db
        self.settings = settings
        self.email_sender = email_sender
        self.google_verifier = google_verifier or GoogleTokenVerifier(
            client_id=settings.google_client_id
        )

    # ── registration ────────────────────────────────────────────────

    async def register(self, *, email: str, password: str) -> User:
        """Create an account and email a verification link.

        ``email_verified`` is always False here: a registration is not proof of
        ownership, and the Google linking rule depends on that distinction.
        """
        existing = await self.user_repo.get_by_email(email)
        if existing is not None:
            raise EmailAlreadyRegisteredError()

        user = await self.user_repo.create(
            email=email,
            password_hash=hash_password(password),
        )
        await self._send_verification(user)
        return user

    async def _send_verification(self, user: User) -> None:
        token = await self.token_repo.issue(
            user_id=user.id,
            purpose=PURPOSE_VERIFY,
            ttl=self.settings.email_token_ttl,
        )
        link = f"{self.settings.public_site_url}/verify-email?token={token}"
        await self.email_sender.send(
            _email(
                to=user.email,
                subject="Verify your Qfza email address",
                body=(
                    "Welcome to Qfza.\n\n"
                    f"Confirm this address to activate your account:\n{link}\n\n"
                    f"This link expires in {self.settings.password_reset_token_ttl_minutes} minutes."
                ),
            )
        )

    async def verify_email(self, *, token: str) -> User:
        """Redeem a verification token."""
        user_id = await self.token_repo.consume(token=token, purpose=PURPOSE_VERIFY)
        if user_id is None:
            raise InvalidTokenError()
        await self.user_repo.mark_email_verified(user_id)
        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise InvalidTokenError()
        return user

    # ── login ───────────────────────────────────────────────────────

    async def login(
        self,
        *,
        email: str,
        password: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Authenticate and open a session.

        Unknown address and wrong password deliberately produce the same error,
        and both spend the same verification time, so neither the message nor
        the latency reveals whether an address is registered.

        Two independent brakes apply. The per-IP ceiling stops one host grinding
        the three-strike lockout against many accounts; the per-account counter
        then locks a single account for good.
        """
        if ip is not None and await self.attempt_repo.is_throttled(
            ip=ip, limit=self.settings.login_ip_hourly_limit
        ):
            raise TooManyAttemptsError()

        user = await self.user_repo.get_by_email(email)

        encoded = user.password_hash if user is not None else _DUMMY_HASH
        password_ok = verify_password(password, encoded)

        if user is None or not password_ok or user.password_hash is None:
            # Still record a failure when the row exists, so the counter cannot
            # be used to probe which addresses are real.
            if user is not None:
                await self._register_failure(user.id)
            if ip is not None:
                await self.attempt_repo.record_failure(ip=ip)
            raise AuthenticationFailedError()

        if user.locked_at is not None:
            raise AccountLockedError()

        if user.email_verified_at is None:
            # Password is correct, so this is not a credential guess. Sign-in
            # still waits on proof of address ownership, which is what the
            # verification link provides.
            raise EmailNotVerifiedError()

        await self.user_repo.clear_failures(user.id)
        if ip is not None:
            await self.attempt_repo.clear(ip=ip)
        return await self._open_session(user, ip=ip, user_agent=user_agent)

    async def _register_failure(self, user_id: uuid.UUID) -> None:
        await self.user_repo.record_failed_login(
            user_id,
            max_attempts=self.settings.max_login_attempts,
        )

    async def _open_session(
        self,
        user: User,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> IssuedSession:
        # The raw token goes to the client exactly once; only its hash is
        # stored, so the cookie cannot be replayed from a database dump.
        raw_token = generate_token()
        expires_at = absolute_deadline(absolute_ttl=self.settings.session_absolute_ttl)
        await self.session_repo.create(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            ip=ip,
            user_agent=user_agent,
        )
        return IssuedSession(user=user, token=raw_token, expires_at=expires_at)

    # ── session resolution ──────────────────────────────────────────

    async def resolve_session(self, token: str) -> AuthIdentity | None:
        """Return the identity for a valid session token, else None.

        Any storage failure raises AuthServiceUnavailableError rather than
        returning None, so an outage denies access instead of looking like a
        logged-out visitor.
        """
        try:
            record = await self.session_repo.get_valid(
                hash_token(token),
                idle_ttl=self.settings.session_idle_ttl,
            )
        except SQLAlchemyError as exc:
            logger.error("Session lookup failed", error=str(exc))
            raise AuthServiceUnavailableError() from exc

        if record is None:
            return None
        user = await self.user_repo.get_by_id(record.user_id)
        if user is None:
            return None
        await self.session_repo.touch(record.id)
        return AuthIdentity(user=user, session_id=record.id)

    async def logout(self, session_id: uuid.UUID) -> None:
        """Revoke the current session."""
        await self.session_repo.revoke(session_id)

    # ── password recovery ───────────────────────────────────────────

    async def request_password_reset(self, *, email: str) -> None:
        """Email a reset link when the address exists.

        A silent no-op for unknown addresses: this endpoint must not become an
        oracle for who has an account.
        """
        user = await self.user_repo.get_by_email(email)
        if user is None:
            logger.info("Password reset requested for unknown address")
            return
        if user.password_hash is None:
            logger.info("Password reset requested for a Google-only account")
            return

        await self.token_repo.invalidate_all_for_user(user_id=user.id, purpose=PURPOSE_RESET)
        token = await self.token_repo.issue(
            user_id=user.id,
            purpose=PURPOSE_RESET,
            ttl=self.settings.email_token_ttl,
        )
        link = f"{self.settings.public_site_url}/reset-password?token={token}"
        await self.email_sender.send(
            _email(
                to=user.email,
                subject="Reset your Qfza password",
                body=(
                    "Someone asked to reset the password for this Qfza account.\n\n"
                    f"Choose a new password here:\n{link}\n\n"
                    f"This link expires in {self.settings.password_reset_token_ttl_minutes} minutes "
                    "and can be used once. If this was not you, no action is needed."
                ),
            )
        )

    async def confirm_password_reset(self, *, token: str, password: str) -> None:
        """Redeem a reset token, set the new password, revoke every session.

        Clearing ``locked_at`` here is what makes a forgotten password
        recoverable despite the permanent lockout.
        """
        user_id = await self.token_repo.consume(token=token, purpose=PURPOSE_RESET)
        if user_id is None:
            raise InvalidTokenError()

        await self.user_repo.update_password(user_id, hash_password(password))
        await self.session_repo.revoke_all_for_user(user_id)

    # ── Google sign-in ──────────────────────────────────────────────

    async def login_with_google(
        self,
        *,
        id_token: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Sign in with a Google ID token, creating the account if new.

        The linking rule is deliberately strict: an existing account is only
        joined to a Google identity when that account's address is already
        verified. An unverified address is somebody's placeholder, and silently
        taking it over would let anyone who registers a victim's address claim
        the account - so that case is refused instead.
        """
        identity = await self.google_verifier.verify_async(id_token)
        if identity is None:
            raise AuthenticationFailedError()

        user = await self.user_repo.get_by_google_sub(identity.subject)
        if user is not None:
            return await self._open_session(user, ip=ip, user_agent=user_agent)

        user = await self.user_repo.get_by_email(identity.email)
        if user is None:
            # First Google sign-in for this address: a new, already-verified
            # account, since Google just proved the address.
            user = await self.user_repo.create(
                email=identity.email,
                password_hash=None,
                google_sub=identity.subject,
            )
            await self.user_repo.mark_email_verified(user.id)
            # mark_email_verified is a bulk UPDATE, so re-read to get a row that
            # reflects it. Without this the response would tell a brand new user
            # their address is unverified.
            user = await self.user_repo.get_by_id(user.id) or user
        elif user.email_verified_at is None:
            logger.info(
                "Refused to link Google to an unverified address",
                user_id=str(user.id),
            )
            raise EmailNotVerifiedError()
        else:
            await self.user_repo.link_google_sub(user.id, identity.subject)
            user.google_sub = identity.subject

        await self.user_repo.clear_failures(user.id)
        return await self._open_session(user, ip=ip, user_agent=user_agent)

    # ── operations ──────────────────────────────────────────────────

    async def sweep_sessions(self) -> int:
        """Delete expired and revoked session rows."""
        return await self.session_repo.sweep_expired()


class HistoryService:
    """Read and delete the caller's own history."""

    def __init__(self, *, history_repo: SearchHistoryRepository) -> None:
        self.history_repo = history_repo

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
        """Record one search. ``user_id`` always comes from the session."""
        return await self.history_repo.record(
            user_id=user_id,
            video_id=video_id,
            keyword=keyword,
            status=status,
            locale=locale,
            source=source,
        )

    async def list_page(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = 20,
        cursor: str | None = None,
    ) -> list:
        return await self.history_repo.list_for_user(user_id=user_id, limit=limit, cursor=cursor)

    async def delete_entry(self, *, user_id: uuid.UUID, entry_id: uuid.UUID) -> bool:
        return await self.history_repo.delete_one(user_id=user_id, entry_id=entry_id)

    async def clear_all(self, *, user_id: uuid.UUID) -> int:
        return await self.history_repo.delete_all_for_user(user_id=user_id)


def _email(*, to: str, subject: str, body: str) -> AuthEmail:
    return AuthEmail(to=to, subject=subject, body=body)
