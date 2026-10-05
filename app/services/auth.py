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
    WrongCurrentPasswordError,
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
from app.repositories.video_repository import VideoRepository
from app.services.email import SmtpEmailSender
from app.services.google import GoogleIdentity, GoogleTokenVerifier
from app.services.history_recorder import STATUS_PROCESSING
from app.services.password import hash_password, verify_password
from app.services.ports import AuthEmail
from app.services.search import SearchService

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
        email_sender: SmtpEmailSender,
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

    async def request_password_set(self, *, user_id: uuid.UUID) -> None:
        """Email a link that lets a password-less account choose a first password.

        Distinct from :meth:`request_password_reset`, which is the *opposite*
        case: that one deliberately refuses an account with no password, so
        reusing it here silently did nothing for exactly the accounts that need
        a link. The address comes from the caller's session rather than the
        request body, so this cannot be pointed at someone else's inbox.

        The link still has to be followed from the mailbox, because the session
        is only a cookie: one XSS should not be enough to keep access after the
        owner closes the tab.
        """
        user = await self.user_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationFailedError()
        if user.password_hash is not None:
            # Already has a password: that is a change, not a first set, and
            # request_password_reset already covers it.
            logger.info("Password set requested for an account that has one", user_id=str(user.id))
            return
        if user.email_verified_at is None:
            # An unverified address proves nothing by mail, since the whole
            # reason it is unverified is that no link ever arrived.
            logger.info("Password set requested for an unverified address", user_id=str(user.id))
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
                subject="Choose your Qfza password",
                body=(
                    "Someone asked to add a password to this Qfza account, which "
                    "currently signs in with Google only.\n\n"
                    f"Choose a password here:\n{link}\n\n"
                    f"This link expires in {self.settings.password_reset_token_ttl_minutes} minutes "
                    "and can be used once. If this was not you, no action is needed and "
                    "your Google sign-in keeps working."
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

    async def change_password(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        current_password: str,
        new_password: str,
        ip: str | None = None,
    ) -> None:
        """Change the caller's own password in place, keeping this session alive.

        The emailed-token flow is the recovery path; this is the ordinary one,
        for someone who is signed in and simply wants a different password. Two
        rules keep it from being weaker than that flow:

        The current password is still required. A live session is not proof of
        ownership on its own - a cookie stolen through XSS must not be enough to
        take the account over permanently - so the caller has to show the thing
        they are about to replace.

        The guess is rate-limited like a sign-in. Without this the endpoint
        becomes an offline oracle: one stolen cookie would otherwise allow
        grinding the current password against the real hash with no lockout and
        no hourly ceiling.

        Every *other* session is revoked, and this one is kept, so the person who
        just proved they own the password is not signed out of the browser they
        proved it in.
        """
        if ip is not None and await self.attempt_repo.is_throttled(
            ip=ip, limit=self.settings.login_ip_hourly_limit
        ):
            raise TooManyAttemptsError()

        user = await self.user_repo.get_by_id(user_id)
        encoded = user.password_hash if user is not None else _DUMMY_HASH

        # The dummy hash is verified against even when there is no password, so a
        # password-less account costs the same time as a wrong guess rather than
        # answering noticeably faster.
        if user is None or encoded is None or not verify_password(current_password, encoded):
            if ip is not None:
                await self.attempt_repo.record_failure(ip=ip)
            raise WrongCurrentPasswordError()

        await self.user_repo.update_password(user_id, hash_password(new_password))
        await self.session_repo.revoke_all_for_user_except(user_id, keep_session_id=session_id)
        if ip is not None:
            await self.attempt_repo.clear(ip=ip)

    # ── Google sign-in ──────────────────────────────────────────────

    async def _sync_full_name(self, user: User, identity: GoogleIdentity) -> None:
        """Copy Google's display name onto the account, if it has one.

        Two rules keep this from destroying data. A claim that arrives empty
        never overwrites a name already stored, because Google omitting a field
        is not a statement that the account has no name. And an unchanged name
        is not written at all, so an ordinary sign-in does not issue an UPDATE.
        """
        name = identity.full_name
        if name is None or name == user.full_name:
            return
        await self.user_repo.set_full_name(user.id, name)
        # set_full_name is a bulk UPDATE that bypasses the identity map, so the
        # in-memory row has to follow or the response still shows the old name.
        user.full_name = name

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
            # Re-read the name every sign-in, so renaming in Google propagates.
            await self._sync_full_name(user, identity)
            return await self._open_session(user, ip=ip, user_agent=user_agent)

        user = await self.user_repo.get_by_email(identity.email)
        if user is None:
            # First Google sign-in for this address: a new, already-verified
            # account, since Google just proved the address.
            user = await self.user_repo.create(
                email=identity.email,
                password_hash=None,
                google_sub=identity.subject,
                full_name=identity.full_name,
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
            await self._sync_full_name(user, identity)

        await self.user_repo.clear_failures(user.id)
        return await self._open_session(user, ip=ip, user_agent=user_agent)

    # ── operations ──────────────────────────────────────────────────

    async def sweep_sessions(self) -> int:
        """Delete expired and revoked session rows."""
        return await self.session_repo.sweep_expired()


#: Most rows a single history page will re-search to recover missing results.
#: Each is one indexed phrase query, so this bounds worst-case page latency
#: without leaving a page's worth of rows permanently unrepaired.
MAX_RESULT_BACKFILLS = 20


#: Extra rows fetched beyond the requested page size so that hiding unresolved
#: rows does not quietly shrink the page. A user who searched several new videos
#: in a row would otherwise see a short first page and an empty-looking history.
HISTORY_PAGE_BUFFER = 20


def _is_showable(entry: SearchHistory) -> bool:
    """Whether a history row has anything worth putting in front of the user.

    A search of a video we have never seen is filed the moment its transcription
    is queued, so it starts with no results to replay and no position to seek to.
    Until the transcript lands there is nothing to show but a bare keyword, so
    those rows stay out of the page and appear once the results arrive.

    ``status`` alone cannot decide this: a queued row is never rewritten when its
    job finishes (see ``routes.py``), so ``processing`` describes how the row was
    filed, not what it now holds. Nor can "has no results" decide it, because a
    genuine miss is recorded the same way - ``routes.py`` files ``not_found``
    with no match list either. What separates them is the pairing: a row is only
    hidden while it is still labelled processing *and* still has nothing to show.
    Once the backfill fills it, or a search genuinely matched nothing, it is
    listed as usual.
    """
    if entry.status != STATUS_PROCESSING:
        return True
    return bool(entry.match_results)


class HistoryService:
    """Read and delete the caller's own history."""

    def __init__(
        self,
        *,
        history_repo: SearchHistoryRepository,
        video_repo: VideoRepository,
        search_service: SearchService,
    ) -> None:
        self.history_repo = history_repo
        self.video_repo = video_repo
        self.search_service = search_service

    async def record(
        self,
        *,
        user_id: uuid.UUID,
        video_id: str,
        keyword: str,
        status: str,
        locale: str | None = None,
        source: str | None = None,
        match_results: list[dict[str, object]] | None = None,
    ) -> SearchHistory:
        """Record one search. ``user_id`` always comes from the session."""
        return await self.history_repo.record(
            user_id=user_id,
            video_id=video_id,
            keyword=keyword,
            status=status,
            locale=locale,
            source=source,
            match_results=match_results,
        )

    async def list_page(
        self,
        *,
        user_id: uuid.UUID,
        limit: int = 20,
        cursor: str | None = None,
    ) -> list:
        """Return one page of history, without the rows that have nothing to show.

        Rows are hidden only after recovery has run, so a search whose transcript
        landed since the last visit is listed on the very first page load instead
        of one load later. The page is over-fetched and then trimmed because
        hiding rows shrinks what a ``limit``-sized fetch returns, and the
        controller reads the page length to decide whether more exist.
        """
        batch = await self.history_repo.list_for_user(
            user_id=user_id, limit=limit + HISTORY_PAGE_BUFFER, cursor=cursor
        )
        await self._recover_missing_results(batch)
        return [entry for entry in batch if _is_showable(entry)][:limit]

    async def _recover_missing_results(self, entries: list[SearchHistory]) -> None:
        """Recover stored results for rows that were filed before they existed.

        A search of a never-before-seen video is recorded the moment the
        transcription is *queued*, so it has no results yet - there is nothing
        to store. Once the transcript lands and the search resolves, nothing was
        left to update that row, so it stayed empty and the history page had no
        choice but to show it as a bare keyword at 00:00.

        The result set is a deterministic function of (video, keyword), so it can
        be recovered after the fact: run the same search again and store
        everything it found. Persisted once, so later page loads pay nothing.

        A position that was already recorded is never moved: that value is a
        deliberate snapshot of where the quote sat when the search ran, and
        re-deriving it here would silently relocate a past result. Only a row
        with no position at all takes one from the recovered matches. Rows from
        before results were stored therefore gain their full list while keeping
        the moment they were already pointing at.

        Deliberately narrow: only rows with no stored results, only videos that
        are actually transcribed, and only when the keyword still matches - a
        keyword that no longer matches had no match to record, and saying so by
        leaving the row empty is honest.
        """
        pending = [entry for entry in entries if not entry.match_results]
        if not pending:
            return
        pending = pending[:MAX_RESULT_BACKFILLS]

        updates: list[tuple[SearchHistory, int, list[dict[str, object]]]] = []
        for entry in pending:
            video = await self.video_repo.get_by_video_id_lite(entry.video_id)
            if video is None or video.transcribed_at is None:
                # Never transcribed (still queued, or failed): there is no
                # transcript to search, so the results are genuinely unknown.
                continue
            matches = await self.search_service.search(video.id, entry.keyword)
            if matches:
                results = [m.model_dump(mode="json") for m in matches]
                # The repository decides whether to write this: None means "no
                # position was ever recorded, take one", a number is a snapshot
                # the search route already committed to and is left alone.
                updates.append(
                    (entry, int(matches[0].progress_seconds), results)
                )

        await self.history_repo.apply_match_results(updates)

    async def delete_entry(self, *, user_id: uuid.UUID, entry_id: uuid.UUID) -> bool:
        return await self.history_repo.delete_one(user_id=user_id, entry_id=entry_id)

    async def clear_all(self, *, user_id: uuid.UUID) -> int:
        return await self.history_repo.delete_all_for_user(user_id=user_id)


def _email(*, to: str, subject: str, body: str) -> AuthEmail:
    return AuthEmail(to=to, subject=subject, body=body)
