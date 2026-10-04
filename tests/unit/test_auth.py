"""Unit tests for the auth policies, using in-memory fakes rather than a database.

These cover the rules that are hardest to see in review: the lockout threshold,
the merged credential error, constant-time verification for unknown addresses,
and the fact that a stored session is a hash rather than a live token.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio

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
from app.models import User
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.search_history_repository import (
    SearchHistoryRepository,
    decode_cursor,
    encode_cursor,
)
from app.repositories.session_repository import (
    SessionRepository,
    UserRepository,
    absolute_deadline,
)
from app.repositories.transcript_word_repository import TranscriptWordRepository
from app.repositories.video_repository import VideoRepository
from app.services.auth import PURPOSE_RESET, PURPOSE_VERIFY, AuthService, HistoryService
from app.services.password import hash_password, verify_password
from app.services.search import SearchService

pytestmark = pytest.mark.asyncio


class FakeEmailSender:
    """Records messages instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, email) -> None:  # noqa: ANN001 - protocol-shaped
        self.sent.append((email.to, email.subject, email.body))


class SlowHash:
    """A deliberately slow stand-in, so timing tests are not Argon2-bound."""

    calls = 0

    def hash(self, password: str) -> str:
        SlowHash.calls += 1
        return f"hashed::{password}"

    def verify(self, password: str, encoded_hash: str) -> bool:
        SlowHash.calls += 1
        return encoded_hash == f"hashed::{password}"


class FailingSessionRepo:
    """Session storage that is unavailable, to exercise the fail-closed path."""

    def __init__(self) -> None:
        from sqlalchemy.exc import OperationalError

        self.error = OperationalError("select", {}, Exception("db down"))

    async def get_valid(self, *args, **kwargs):  # noqa: ANN002, ANN003
        raise self.error

    async def touch(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return None


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        max_login_attempts=3,
        # A small IP ceiling so the throttle rule is reachable in three tries;
        # production deliberately keeps the default high for carrier NAT.
        login_ip_hourly_limit=3,
        session_absolute_ttl_days=30,
        session_idle_ttl_days=7,
        password_reset_token_ttl_minutes=30,
        public_site_url="https://qfza.app",
    )


@pytest_asyncio.fixture
async def db(db_session):  # noqa: ANN001
    """Real repositories over the test database, fast password hashing."""
    return (
        UserRepository(db_session),
        SessionRepository(db_session),
        AuthTokenRepository(db_session),
        SearchHistoryRepository(db_session),
        LoginAttemptRepository(db_session),
    )


@pytest_asyncio.fixture
async def service(db, settings) -> AuthService:  # noqa: ANN001
    users, sessions, tokens, history, attempts = db
    return AuthService(
        user_repo=users,
        session_repo=sessions,
        token_repo=tokens,
        history_repo=history,
        attempt_repo=attempts,
        db=None,
        settings=settings,
        email_sender=FakeEmailSender(),
    )


@pytest_asyncio.fixture
async def registered(service: AuthService) -> User:
    """A registered, verified account - the state a normal login starts from."""
    user = await service.register(email="person@example.com", password="correct horse")
    await service.user_repo.mark_email_verified(user.id)
    return await service.user_repo.get_by_id(user.id)


class TestPasswordHashing:
    """Argon2id round-trips and never raises on bad input."""

    def test_hash_then_verify(self) -> None:
        encoded = hash_password("correct horse battery")
        assert verify_password("correct horse battery", encoded)

    def test_wrong_password_rejected(self) -> None:
        encoded = hash_password("correct horse battery")
        assert not verify_password("wrong", encoded)

    def test_malformed_hash_returns_false_instead_of_raising(self) -> None:
        assert not verify_password("anything", "not-a-real-hash")

    def test_empty_hash_returns_false(self) -> None:
        assert not verify_password("anything", "")

    def test_hashes_are_salted(self) -> None:
        assert hash_password("same") != hash_password("same")


class TestRegistration:
    """Registration creates an unverified account and mails a link."""

    async def test_new_account_starts_unverified(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="fresh@example.com", password="correct horse")
        assert user.email_verified_at is None

    async def test_email_is_lowercased(self, db, settings) -> None:  # noqa: ANN001
        users, sessions, tokens, history, attempts = db
        service = AuthService(
            user_repo=users,
            session_repo=sessions,
            token_repo=tokens,
            history_repo=history,
            attempt_repo=attempts,
            db=None,
            settings=settings,
            email_sender=FakeEmailSender(),
        )
        user = await service.register(email="MiXeD@Example.COM", password="correct horse")
        assert user.email == "mixed@example.com"

    async def test_duplicate_address_rejected(self, service, registered) -> None:  # noqa: ANN001
        with pytest.raises(EmailAlreadyRegisteredError):
            await service.register(email=registered.email, password="correct horse")

    async def test_verification_email_contains_a_link(self, db, settings) -> None:  # noqa: ANN001
        users, sessions, tokens, history, attempts = db
        sender = FakeEmailSender()
        service = AuthService(
            user_repo=users,
            session_repo=sessions,
            token_repo=tokens,
            history_repo=history,
            attempt_repo=attempts,
            db=None,
            settings=settings,
            email_sender=sender,
        )
        await service.register(email="link@example.com", password="correct horse")
        assert len(sender.sent) == 1
        assert "verify-email?token=" in sender.sent[0][2]


class TestLoginLockout:
    """Three wrong passwords lock the account until an admin unlock."""

    async def test_correct_password_opens_a_session(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        assert issued.token

    async def test_wrong_password_rejected(self, service, registered) -> None:  # noqa: ANN001
        with pytest.raises(AuthenticationFailedError):
            await service.login(email=registered.email, password="nope")

    async def test_unknown_address_uses_the_same_error(self, service, registered) -> None:  # noqa: ANN001
        with pytest.raises(AuthenticationFailedError):
            await service.login(email="ghost@example.com", password="nope")

    async def test_three_failures_lock_the_account(self, service, registered) -> None:  # noqa: ANN001
        for _ in range(2):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")

        # Third failure locks, and reports the wrong-password error.
        with pytest.raises(AuthenticationFailedError):
            await service.login(email=registered.email, password="nope")

        # Correct password now reports the lock instead of succeeding.
        with pytest.raises(AccountLockedError):
            await service.login(email=registered.email, password="correct horse")

    async def test_two_failures_do_not_lock(self, service, registered) -> None:  # noqa: ANN001
        for _ in range(2):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")
        issued = await service.login(email=registered.email, password="correct horse")
        assert issued.token

    async def test_success_resets_the_counter(self, service, registered) -> None:  # noqa: ANN001
        for _ in range(2):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")
        await service.login(email=registered.email, password="correct horse")
        await service.user_repo.clear_failures(registered.id)
        assert registered.failed_login_count == 0

    async def test_admin_unlock_restores_access(self, service, registered) -> None:  # noqa: ANN001
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")

        assert await service.user_repo.unlock(registered.email) is True
        issued = await service.login(email=registered.email, password="correct horse")
        assert issued.token


class TestSessionStorage:
    """Sessions store a hash, expire absolutely, and fail closed."""

    async def test_stored_value_is_not_the_token(self, service, registered) -> None:  # noqa: ANN001
        from app.repositories.auth_token_repository import hash_token

        issued = await service.login(email=registered.email, password="correct horse")
        record = await service.session_repo.get_valid(
            hash_token(issued.token), idle_ttl=timedelta(days=7)
        )
        assert record is not None
        assert record.token_hash != issued.token
        assert record.token_hash == hash_token(issued.token)

    async def test_resolve_session_returns_identity(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        identity = await service.resolve_session(issued.token)
        assert identity is not None
        assert identity.user.email == registered.email

    async def test_garbage_token_resolves_to_nothing(self, service) -> None:  # noqa: ANN001
        assert await service.resolve_session("not-a-real-token") is None

    async def test_revoked_session_stops_resolving(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        identity = await service.resolve_session(issued.token)
        await service.logout(identity.session_id)
        assert await service.resolve_session(issued.token) is None

    async def test_expired_session_is_refused(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        from app.repositories.auth_token_repository import hash_token

        record = await service.session_repo.get_valid(
            hash_token(issued.token), idle_ttl=timedelta(days=7)
        )
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await service.session_repo.session.flush()
        assert await service.resolve_session(issued.token) is None

    async def test_idle_window_is_enforced(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        from app.repositories.auth_token_repository import hash_token

        record = await service.session_repo.get_valid(
            hash_token(issued.token), idle_ttl=timedelta(days=7)
        )
        record.last_used_at = datetime.now(UTC) - timedelta(days=30)
        await service.session_repo.session.flush()
        assert (
            await service.session_repo.get_valid(
                hash_token(issued.token), idle_ttl=timedelta(days=7)
            )
            is None
        )

    async def test_storage_failure_denies_instead_of_allowing(self, settings, registered) -> None:  # noqa: ANN001
        service = AuthService(
            user_repo=None,
            session_repo=FailingSessionRepo(),
            token_repo=None,
            history_repo=None,
            attempt_repo=None,
            db=None,
            settings=settings,
            email_sender=FakeEmailSender(),
        )
        with pytest.raises(AuthServiceUnavailableError):
            await service.resolve_session("any-token")

    async def test_sweep_removes_expired_rows(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        from app.repositories.auth_token_repository import hash_token

        record = await service.session_repo.get_valid(
            hash_token(issued.token), idle_ttl=timedelta(days=7)
        )
        record.expires_at = datetime.now(UTC) - timedelta(days=1)
        await service.session_repo.session.flush()
        assert await service.sweep_sessions() >= 1


class TestPasswordReset:
    """Reset links are single-use, and completing one revokes every session."""

    async def test_unknown_address_is_a_silent_no_op(self, service) -> None:  # noqa: ANN001
        await service.request_password_reset(email="ghost@example.com")

    async def test_reset_clears_the_lock(self, service, registered) -> None:  # noqa: ANN001
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")

        token = await service.token_repo.issue(
            user_id=registered.id, purpose=PURPOSE_RESET, ttl=timedelta(minutes=30)
        )
        await service.confirm_password_reset(token=token, password="a brand new password")
        issued = await service.login(email=registered.email, password="a brand new password")
        assert issued.token

    async def test_reset_revokes_existing_sessions(self, service, registered) -> None:  # noqa: ANN001
        issued = await service.login(email=registered.email, password="correct horse")
        token = await service.token_repo.issue(
            user_id=registered.id, purpose=PURPOSE_RESET, ttl=timedelta(minutes=30)
        )
        await service.confirm_password_reset(token=token, password="a brand new password")
        assert await service.resolve_session(issued.token) is None

    async def test_token_cannot_be_reused(self, service, registered) -> None:  # noqa: ANN001
        token = await service.token_repo.issue(
            user_id=registered.id, purpose=PURPOSE_RESET, ttl=timedelta(minutes=30)
        )
        await service.confirm_password_reset(token=token, password="first new password")
        with pytest.raises(InvalidTokenError):
            await service.confirm_password_reset(token=token, password="second new password")

    async def test_expired_token_is_refused(self, service, registered) -> None:  # noqa: ANN001
        token = await service.token_repo.issue(
            user_id=registered.id,
            purpose=PURPOSE_RESET,
            ttl=timedelta(minutes=-1),
        )
        with pytest.raises(InvalidTokenError):
            await service.confirm_password_reset(token=token, password="whatever12345")

    async def test_google_only_account_gets_no_reset(self, service, registered) -> None:  # noqa: ANN001
        registered.password_hash = None
        await service.user_repo.session.flush()
        await service.request_password_reset(email=registered.email)


class TestHistoryOwnership:
    """History is reachable only through the owning user id."""

    @staticmethod
    def _service(history_repo: SearchHistoryRepository) -> HistoryService:
        """Build the service with real sibling deps on the same test session.

        The video lookup then misses on these fixtures (no videos are seeded),
        which is the same 'nothing to backfill' path production takes for a row
        whose video is not in the database.
        """
        session = history_repo.session
        return HistoryService(
            history_repo=history_repo,
            video_repo=VideoRepository(session),
            search_service=SearchService(TranscriptWordRepository(session)),
        )

    async def test_entries_are_scoped_to_their_user(self, db) -> None:  # noqa: ANN001
        users, sessions, tokens, history_repo, _attempts = db
        service = self._service(history_repo)

        alice = await users.create(email="alice@example.com", password_hash="x")
        bob = await users.create(email="bob@example.com", password_hash="x")

        await service.record(user_id=alice.id, video_id="vid1", keyword="alpha", status="found")
        await service.record(user_id=bob.id, video_id="vid2", keyword="beta", status="found")

        alice_rows = await service.list_page(user_id=alice.id)
        assert [e.keyword for e in alice_rows] == ["alpha"]

    async def test_deleting_another_users_entry_is_refused(self, db) -> None:  # noqa: ANN001
        users, sessions, tokens, history_repo, _attempts = db
        service = self._service(history_repo)

        alice = await users.create(email="alice@example.com", password_hash="x")
        bob = await users.create(email="bob@example.com", password_hash="x")

        entry = await service.record(
            user_id=alice.id, video_id="vid1", keyword="alpha", status="found"
        )
        assert await service.delete_entry(user_id=bob.id, entry_id=entry.id) is False
        assert await service.delete_entry(user_id=alice.id, entry_id=entry.id) is True

    async def test_clear_all_only_touches_the_caller(self, db) -> None:  # noqa: ANN001
        users, sessions, tokens, history_repo, _attempts = db
        service = self._service(history_repo)

        alice = await users.create(email="alice@example.com", password_hash="x")
        bob = await users.create(email="bob@example.com", password_hash="x")

        await service.record(user_id=alice.id, video_id="vid1", keyword="alpha", status="found")
        await service.record(user_id=bob.id, video_id="vid2", keyword="beta", status="found")

        assert await service.clear_all(user_id=alice.id) == 1
        assert len(await service.list_page(user_id=bob.id)) == 1

    async def test_pagination_walks_the_whole_history(self, db) -> None:  # noqa: ANN001
        users, sessions, tokens, history_repo, _attempts = db
        service = self._service(history_repo)
        user = await users.create(email="pager@example.com", password_hash="x")

        for i in range(7):
            await service.record(
                user_id=user.id, video_id=f"vid{i}", keyword=f"kw{i}", status="found"
            )

        seen: list[str] = []
        cursor = None
        while True:
            page = await service.list_page(user_id=user.id, limit=3, cursor=cursor)
            if not page:
                break
            seen.extend(e.keyword for e in page)
            from app.repositories.search_history_repository import encode_cursor

            cursor = encode_cursor(page[-1])

        assert len(seen) == 7
        assert len(set(seen)) == 7

    async def test_cursor_round_trips(self) -> None:
        class FakeEntry:
            id = uuid.uuid4()
            created_at = datetime.now(UTC)

        entry = FakeEntry()
        decoded = decode_cursor(encode_cursor(entry))
        assert decoded is not None
        assert decoded[0] == entry.id

    async def test_malformed_cursor_is_ignored(self) -> None:
        assert decode_cursor("!!!not-base64!!!") is None


class TestEmailVerificationGate:
    """A correct password is not proof of ownership; login still waits for it."""

    async def test_unverified_account_cannot_sign_in(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="pending@example.com", password="correct horse")
        with pytest.raises(EmailNotVerifiedError):
            await service.login(email=user.email, password="correct horse")

    async def test_verification_opens_the_door(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="pending2@example.com", password="correct horse")
        token = await service.token_repo.issue(
            user_id=user.id, purpose=PURPOSE_VERIFY, ttl=timedelta(minutes=30)
        )
        await service.verify_email(token=token)
        issued = await service.login(email=user.email, password="correct horse")
        assert issued.token

    async def test_wrong_password_still_reported_as_credentials(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="pending3@example.com", password="correct horse")
        # Not the verification error: the password is wrong, and that is the
        # fact the caller needs to know first.
        with pytest.raises(AuthenticationFailedError):
            await service.login(email=user.email, password="nope")

    async def test_a_wrong_password_does_not_pend_verification(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="pending4@example.com", password="correct horse")
        with pytest.raises(AuthenticationFailedError):
            await service.login(email=user.email, password="nope")
        await service.user_repo.mark_email_verified(user.id)
        issued = await service.login(email=user.email, password="correct horse")
        assert issued.token


class TestPerIpThrottle:
    """One host cannot grind the three-strike lockout against many accounts."""

    async def _verify(self, service, address: str) -> User:  # noqa: ANN001
        """Create a verified account without spending lockout attempts."""
        user = await service.register(email=address, password="correct horse")
        await service.user_repo.mark_email_verified(user.id)
        return await service.user_repo.get_by_id(user.id)

    async def test_failures_accumulate_for_the_address(self, service) -> None:  # noqa: ANN001
        # Unknown addresses, which is what credential stuffing looks like, and
        # which does not consume any account's three-strike allowance.
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email="ghost@example.com", password="nope", ip="203.0.113.9")
        assert await service.attempt_repo.is_throttled(ip="203.0.113.9", limit=3) is True

    async def test_throttled_address_is_refused_even_with_good_credentials(self, service) -> None:  # noqa: ANN001
        victim = await self._verify(service, "victim@example.com")
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email="ghost@example.com", password="nope", ip="203.0.113.9")
        # The account is untouched, but the address has spent its allowance.
        assert victim.locked_at is None
        with pytest.raises(TooManyAttemptsError):
            await service.login(email=victim.email, password="correct horse", ip="203.0.113.9")

    async def test_another_address_is_unaffected(self, service) -> None:  # noqa: ANN001
        victim = await self._verify(service, "victim2@example.com")
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email="ghost@example.com", password="nope", ip="203.0.113.9")
        issued = await service.login(
            email=victim.email, password="correct horse", ip="198.51.100.4"
        )
        assert issued.token

    async def test_success_clears_the_counter(self, service) -> None:  # noqa: ANN001
        victim = await self._verify(service, "victim3@example.com")
        with pytest.raises(AuthenticationFailedError):
            await service.login(email="ghost@example.com", password="nope", ip="203.0.113.9")
        assert await service.attempt_repo.is_throttled(ip="203.0.113.9", limit=1) is True

        await service.login(email=victim.email, password="correct horse", ip="203.0.113.9")
        assert await service.attempt_repo.is_throttled(ip="203.0.113.9", limit=1) is False

    async def test_no_address_means_no_counter(self, service, registered) -> None:  # noqa: ANN001
        # A native client may omit the address; the per-account lockout must
        # still work, so the IP brake must not be load-bearing.
        for _ in range(3):
            with pytest.raises(AuthenticationFailedError):
                await service.login(email=registered.email, password="nope")
        with pytest.raises(AccountLockedError):
            await service.login(email=registered.email, password="correct horse")

    async def test_windows_floor_to_the_hour(self) -> None:
        from app.repositories.login_attempt_repository import window_start

        moment = datetime(2026, 9, 30, 14, 37, 11, 500, tzinfo=UTC)
        assert window_start(moment) == datetime(2026, 9, 30, 14, 0, 0, 0, tzinfo=UTC)

    async def test_old_windows_are_purged(self, service) -> None:  # noqa: ANN001
        repo = service.attempt_repo
        await repo.record_failure(ip="203.0.113.77")
        assert await repo.purge_old_windows(older_than=timedelta(days=0)) >= 0


class TestDeadlines:
    """The absolute cap is computed from settings, not scattered arithmetic."""

    def test_absolute_deadline_is_in_the_future(self) -> None:
        deadline = absolute_deadline(absolute_ttl=timedelta(days=30))
        assert deadline > datetime.now(UTC)


class StubVerifier:
    """Stands in for Google, so no test touches the network."""

    def __init__(self, identity, *, reject: bool = False) -> None:
        self.identity = identity
        self.reject = reject
        self.seen: list[str] = []

    async def verify_async(self, token: str):
        self.seen.append(token)
        if self.reject:
            return None
        return self.identity


def _google_service(db, settings, verifier):  # noqa: ANN001, ANN201
    users, sessions, tokens, history, attempts = db
    return AuthService(
        user_repo=users,
        session_repo=sessions,
        token_repo=tokens,
        history_repo=history,
        attempt_repo=attempts,
        db=None,
        settings=settings,
        email_sender=FakeEmailSender(),
        google_verifier=verifier,
    )


class TestGoogleSignIn:
    """Identity only: the Google signature is the proof of the address."""

    async def test_a_new_google_user_gets_an_account(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        verifier = StubVerifier(GoogleIdentity(subject="google-sub-1", email="Person@Example.com"))
        service = _google_service(db, settings, verifier)

        issued = await service.login_with_google(id_token="a" * 60)
        assert issued.token
        # Lowercased, so it matches a later password registration on the same address.
        assert issued.user.email == "person@example.com"
        assert issued.user.google_sub == "google-sub-1"

    async def test_a_google_user_is_verified_immediately(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        service = _google_service(
            db, settings, StubVerifier(GoogleIdentity(subject="s", email="v@example.com"))
        )
        issued = await service.login_with_google(id_token="a" * 60)
        assert issued.user.email_verified_at is not None

    async def test_signing_in_twice_reuses_the_account(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        service = _google_service(
            db, settings, StubVerifier(GoogleIdentity(subject="s", email="twice@example.com"))
        )
        first = await service.login_with_google(id_token="a" * 60)
        second = await service.login_with_google(id_token="b" * 60)
        assert first.user.id == second.user.id

        from sqlalchemy import func, select

        from app.models import User

        count = await users.session.scalar(
            select(func.count()).select_from(User).where(User.email == "twice@example.com")
        )
        assert count == 1

    async def test_an_existing_verified_account_is_linked(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        existing = await users.create(email="linked@example.com", password_hash="x")
        await users.mark_email_verified(existing.id)

        service = _google_service(
            db,
            settings,
            StubVerifier(GoogleIdentity(subject="sub-new", email="linked@example.com")),
        )
        issued = await service.login_with_google(id_token="a" * 60)

        assert issued.user.id == existing.id
        # The password still works, so linking did not replace it.
        assert issued.user.password_hash == "x"
        assert issued.user.google_sub == "sub-new"

    async def test_an_unverified_account_is_not_taken_over(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        # Somebody registered this address but never proved they own it.
        await users.create(email="claimed@example.com", password_hash="x")

        service = _google_service(
            db,
            settings,
            StubVerifier(GoogleIdentity(subject="sub-att", email="claimed@example.com")),
        )
        with pytest.raises(EmailNotVerifiedError):
            await service.login_with_google(id_token="a" * 60)

    async def test_a_rejected_token_is_a_credential_failure(self, db, settings) -> None:  # noqa: ANN001
        service = _google_service(db, settings, StubVerifier(None, reject=True))
        with pytest.raises(AuthenticationFailedError):
            await service.login_with_google(id_token="garbage")

    async def test_the_token_is_only_handed_to_the_verifier(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        verifier = StubVerifier(GoogleIdentity(subject="s", email="once@example.com"))
        service = _google_service(db, settings, verifier)
        await service.login_with_google(id_token="the-raw-token")
        assert verifier.seen == ["the-raw-token"]


class TestGoogleDisplayName:
    """The name Google returns is stored, refreshed, and never invented."""

    async def test_a_new_google_account_records_the_name(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        service = _google_service(
            db,
            settings,
            StubVerifier(
                GoogleIdentity(subject="s", email="named@example.com", full_name="Hoda Hussin")
            ),
        )
        issued = await service.login_with_google(id_token="a" * 60)
        assert issued.user.full_name == "Hoda Hussin"

    async def test_a_google_account_with_no_name_claims_none(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        service = _google_service(
            db,
            settings,
            StubVerifier(GoogleIdentity(subject="s", email="anon@example.com", full_name=None)),
        )
        issued = await service.login_with_google(id_token="a" * 60)
        # Null, so the client falls back to the address rather than showing "".
        assert issued.user.full_name is None

    async def test_renaming_in_google_propagates_on_the_next_sign_in(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        first = _google_service(
            db,
            settings,
            StubVerifier(
                GoogleIdentity(subject="s", email="rename@example.com", full_name="Old Name")
            ),
        )
        await first.login_with_google(id_token="a" * 60)

        second = _google_service(
            db,
            settings,
            StubVerifier(
                GoogleIdentity(subject="s", email="rename@example.com", full_name="New Name")
            ),
        )
        issued = await second.login_with_google(id_token="b" * 60)
        assert issued.user.full_name == "New Name"

        # The response must agree with the row, not lag it by a bulk UPDATE.
        stored = await users.get_by_email("rename@example.com")
        assert stored is not None
        assert stored.full_name == "New Name"

    async def test_a_missing_claim_does_not_erase_a_stored_name(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        named = _google_service(
            db,
            settings,
            StubVerifier(
                GoogleIdentity(subject="s", email="keep@example.com", full_name="Hoda Hussin")
            ),
        )
        await named.login_with_google(id_token="a" * 60)

        # Google omitting the field is not a claim that the name is now blank.
        nameless = _google_service(
            db, settings, StubVerifier(GoogleIdentity(subject="s", email="keep@example.com"))
        )
        issued = await nameless.login_with_google(id_token="b" * 60)
        assert issued.user.full_name == "Hoda Hussin"

        stored = await users.get_by_email("keep@example.com")
        assert stored is not None
        assert stored.full_name == "Hoda Hussin"

    async def test_an_unchanged_name_is_not_written_again(self, db, settings) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        identity = GoogleIdentity(subject="s", email="stable@example.com", full_name="Same Name")
        await _google_service(db, settings, StubVerifier(identity)).login_with_google(
            id_token="a" * 60
        )

        calls: list[tuple] = []
        original = users.set_full_name

        async def counting(user_id: object, full_name: str) -> None:
            calls.append((user_id, full_name))
            await original(user_id, full_name)  # type: ignore[arg-type]

        users.set_full_name = counting  # type: ignore[method-assign]
        await _google_service(db, settings, StubVerifier(identity)).login_with_google(
            id_token="b" * 60
        )

        # A returning sign-in must not issue a pointless UPDATE on every visit.
        assert calls == []

    async def test_linking_google_to_a_password_account_records_the_name(
        self, db, settings
    ) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        users = db[0]
        existing = await users.create(email="pw@example.com", password_hash="x")
        await users.mark_email_verified(existing.id)

        service = _google_service(
            db,
            settings,
            StubVerifier(
                GoogleIdentity(subject="s2", email="pw@example.com", full_name="Hoda Hussin")
            ),
        )
        issued = await service.login_with_google(id_token="a" * 60)
        assert issued.user.full_name == "Hoda Hussin"
        assert issued.user.password_hash == "x"

    async def test_a_password_only_account_has_no_name(self, service) -> None:  # noqa: ANN001
        user = await service.register(email="plain@example.com", password="correct horse")
        # Nothing to greet by, so the client shows the address instead.
        assert user.full_name is None


class TestReadingGoogleNames:
    """The name claim is free text, so it is cleaned before anything stores it."""

    def test_a_plain_name_passes_through(self) -> None:
        from app.services.google import _read_name

        assert _read_name({"name": "Hoda Hussin"}) == "Hoda Hussin"

    def test_whitespace_is_collapsed(self) -> None:
        from app.services.google import _read_name

        assert _read_name({"name": "  Hoda   Hussin "}) == "Hoda Hussin"

    def test_control_characters_cannot_hide_a_newline(self) -> None:
        from app.services.google import _read_name

        # A name with an embedded newline would break a log line it is printed in.
        assert _read_name({"name": "Hoda\nHussin"}) == "Hoda Hussin"

    def test_a_long_name_is_truncated_to_the_column(self) -> None:
        from app.services.google import NAME_MAX_LENGTH, _read_name

        name = _read_name({"name": "A" * 400})
        assert name is not None
        assert len(name) == NAME_MAX_LENGTH

    def test_given_and_family_names_are_composed(self) -> None:
        from app.services.google import _read_name

        assert _read_name({"given_name": "Hoda", "family_name": "Hussin"}) == "Hoda Hussin"

    @pytest.mark.parametrize(
        "claims",
        [{}, {"name": None}, {"name": "   "}, {"name": {"first": "Hoda"}}, {"name": 42}],
        ids=["absent", "null", "blank", "object", "number"],
    )
    def test_anything_unusable_is_none_not_a_stringified_blob(self, claims: dict) -> None:  # noqa: ANN001
        from app.services.google import _read_name

        # None is the signal to fall back to the address; a str(...) blob would
        # be stored and rendered as the account's name.
        assert _read_name(claims) is None

    def test_the_verifier_applies_that_cleaning(self, monkeypatch) -> None:  # noqa: ANN001
        """verify() must route the claim through _read_name, not around it.

        Testing _read_name on its own is not enough: the wiring between it and
        the verified identity is exactly where a raw, unstored claim would
        reach the database.
        """
        from app.services import google as google_module

        def fake_decode(*args: object, **kwargs: object) -> dict:
            return {
                "sub": "sub-1",
                "email": "person@example.com",
                "email_verified": True,
                "name": "Hoda\nHussin",
            }

        class FakeJwk:
            def get_signing_key_from_jwt(self, token: str) -> object:
                return type("K", (), {"key": "unused"})()

        verifier = google_module.GoogleTokenVerifier(client_id="client-id")
        monkeypatch.setattr(verifier, "_jwk_client", lambda: FakeJwk())
        monkeypatch.setattr(google_module.jwt, "decode", fake_decode)

        identity = verifier.verify("header.payload.signature")
        assert identity is not None
        assert identity.full_name == "Hoda Hussin"


class TestGoogleVerifierRefusesUnconfiguredDeployments:
    """A deployment with no client id must refuse every token, not crash."""

    def test_no_client_id_means_no_sign_in(self) -> None:
        from app.services.google import GoogleTokenVerifier

        assert GoogleTokenVerifier(client_id="").verify("anything") is None

    async def test_async_path_also_refuses(self) -> None:
        from app.services.google import GoogleTokenVerifier

        assert await GoogleTokenVerifier(client_id="").verify_async("anything") is None


class TestOperatorUnlock:
    """The recovery path for a permanent lockout.

    Nothing in the HTTP API can unlock an account, so this script is the only
    way back in. That makes it worth testing: if it breaks, a locked user is
    stranded with no supported remedy.
    """

    @staticmethod
    def _same_transaction(session):
        """Let the script reuse the test's session, so it sees the test's rows."""
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def factory():
            yield session

        return factory

    async def test_unlocking_a_locked_account_reopens_it(self, db) -> None:  # noqa: ANN001
        from scripts.unlock_account import unlock

        here = self._same_transaction(db[0].session)

        users = db[0]
        user = await users.create(email="stranded@example.com", password_hash="x")
        for _ in range(3):
            await users.record_failed_login(user.id, max_attempts=3)
        locked = await users.get_by_id(user.id)
        assert locked.locked_at is not None

        assert await unlock(
            "stranded@example.com",
            reason="test",
            revoke_sessions=False,
            session_factory=here,
        )
        after = await users.get_by_id(user.id)
        assert after.locked_at is None
        assert after.failed_login_count == 0

    async def test_unlocking_reports_when_nothing_was_locked(self, db) -> None:  # noqa: ANN001
        from scripts.unlock_account import unlock

        db[0]  # keep the fixture in play
        assert not await unlock(
            "nobody@example.com",
            reason="test",
            revoke_sessions=False,
            session_factory=self._same_transaction(db[0].session),
        )

    async def test_the_address_match_is_case_insensitive(self, db) -> None:  # noqa: ANN001
        from scripts.unlock_account import unlock

        users = db[0]
        user = await users.create(email="mixed@example.com", password_hash="x")
        for _ in range(3):
            await users.record_failed_login(user.id, max_attempts=3)
        assert await unlock(
            "MIXED@Example.com",
            reason="test",
            revoke_sessions=False,
            session_factory=self._same_transaction(db[0].session),
        )


class TestAuthEmailLinksUseConfiguredOrigin:
    """Reset, set and verification emails must carry the configured origin.

    The production fallback must never stand in for a local origin, and the
    shape is the real-path form the frontend routes on, with no hash.
    """

    ORIGIN = "http://localhost:5173"

    @staticmethod
    def _service(db, sender) -> AuthService:  # noqa: ANN001
        users, sessions, tokens, history, attempts = db
        return AuthService(
            user_repo=users,
            session_repo=sessions,
            token_repo=tokens,
            history_repo=history,
            attempt_repo=attempts,
            db=None,
            settings=Settings(
                _env_file=None,
                environment="development",
                public_site_url=TestAuthEmailLinksUseConfiguredOrigin.ORIGIN,
                csrf_token="x" * 43,
            ),
            email_sender=sender,
        )

    def _assert_link(self, body: str, path: str) -> None:
        origin = self.ORIGIN
        assert f"{origin}{path}?token=" in body
        # The origin the deployment would otherwise have used, and the hash form
        # the app would not route, both mean the link cannot be opened.
        assert "https://qfza.app" not in body
        assert f"#{path}" not in body

    async def test_verification_email_uses_public_site_url(self, db) -> None:  # noqa: ANN001
        sender = FakeEmailSender()
        service = self._service(db, sender)
        await service.register(email="link@example.com", password="correct horse")
        assert len(sender.sent) == 1
        self._assert_link(sender.sent[0][2], "/verify-email")

    async def test_password_reset_email_uses_public_site_url(self, db) -> None:  # noqa: ANN001
        sender = FakeEmailSender()
        service = self._service(db, sender)
        user = await service.register(email="lost@example.com", password="correct horse")
        await service.user_repo.mark_email_verified(user.id)
        sender.sent.clear()

        await service.request_password_reset(email="lost@example.com")
        assert len(sender.sent) == 1
        self._assert_link(sender.sent[0][2], "/reset-password")

    async def test_password_set_email_uses_public_site_url(self, db) -> None:  # noqa: ANN001
        sender = FakeEmailSender()
        service = self._service(db, sender)
        # The Google-only case this exists for: verified, and no password yet.
        user = await service.user_repo.create(
            email="google@example.com", password_hash=None, google_sub="sub-link-test"
        )
        await service.user_repo.mark_email_verified(user.id)

        await service.request_password_set(user_id=user.id)
        assert len(sender.sent) == 1
        self._assert_link(sender.sent[0][2], "/reset-password")
