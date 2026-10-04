"""Giving a password-less (Google-only) account its first password.

The bug these guard against: ``/password-reset`` was originally reused for this
job, and it deliberately refuses an account with no password - the inverse of
who needs the link. The result was a 202 that looked like success, no token, and
no mail. Mocking the API hid it, because the mock happily accepted the request
the real service silently ignored.

So these run the real service over the real database. The assertions that matter
are about tokens and rows on disk, not about a stub having been called.
"""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.exceptions import AuthenticationFailedError, InvalidTokenError
from app.main import create_app
from app.repositories.auth_token_repository import AuthTokenRepository, hash_token
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.search_history_repository import SearchHistoryRepository
from app.repositories.session_repository import SessionRepository, UserRepository
from app.services.auth import PURPOSE_RESET, AuthService
from app.services.password import verify_password
from tests.unit.test_auth import FakeEmailSender

pytestmark = pytest.mark.asyncio


@pytest.fixture
def settings() -> Settings:
    """Same shape as the unit suite: fast hashing, no real secrets."""
    return Settings(
        _env_file=None,
        environment="test",
        max_login_attempts=3,
        login_ip_hourly_limit=3,
        public_site_url="https://qfza.app",
    )


TOKEN_IN_BODY = re.compile(r"/reset-password\?token=([A-Za-z0-9_-]+)")


async def _token_rows(db_session, user_id):  # noqa: ANN001
    """Read the token rows straight from the table under test.

    Asserted against the database rather than a repository mock: the original
    bug was a real service declining to write a row, which a mock cannot show.
    """
    from sqlalchemy import select

    from app.models import AuthToken

    result = await db_session.execute(select(AuthToken).where(AuthToken.user_id == user_id))
    return list(result.scalars())


@pytest_asyncio.fixture
async def repos(db_session):  # noqa: ANN001
    return (
        UserRepository(db_session),
        SessionRepository(db_session),
        AuthTokenRepository(db_session),
        SearchHistoryRepository(db_session),
        LoginAttemptRepository(db_session),
    )


@pytest_asyncio.fixture
async def sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest_asyncio.fixture
async def service(repos, settings, sender) -> AuthService:  # noqa: ANN001
    users, sessions, tokens, history, attempts = repos
    return AuthService(
        user_repo=users,
        session_repo=sessions,
        token_repo=tokens,
        history_repo=history,
        attempt_repo=attempts,
        db=None,
        settings=settings,
        email_sender=sender,
    )


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        session_cookie_name="jumpto_session",
        csrf_token="csrf-secret-for-tests",
        public_site_url="https://qfza.app",
    )


@pytest_asyncio.fixture
async def auth_client(db_session, auth_settings, sender):  # noqa: ANN001
    """HTTP client wired to the test settings and the capturing sender.

    Mirrors tests/integration/test_auth_api.py so the route is exercised through
    the real dependency graph, CSRF guard included.
    """

    async def override_get_db():
        yield db_session
        await db_session.commit()

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_settings] = lambda: auth_settings
    import app.api.v1_auth as v1_auth

    app.dependency_overrides[v1_auth.get_email_sender] = lambda: sender
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def google_only_user(service: AuthService, email: str = "g@example.com"):
    """The state Google sign-in leaves behind: verified, no password."""
    user = await service.user_repo.create(email=email, password_hash=None, google_sub="sub-1")
    await service.user_repo.mark_email_verified(user.id)
    return await service.user_repo.get_by_id(user.id)


class TestRequestPasswordSet:
    async def test_a_google_only_account_gets_a_token_and_an_email(
        self, service: AuthService, sender: FakeEmailSender, db_session
    ) -> None:
        # The exact case that failed silently: no password, so the reset
        # endpoint bailed and the profile button was a no-op.
        user = await google_only_user(service)

        await service.request_password_set(user_id=user.id)

        assert len(sender.sent) == 1
        to, subject, body = sender.sent[0]
        assert to == "g@example.com"
        assert TOKEN_IN_BODY.search(body), "the email must carry a redeemable link"

        rows = await _token_rows(db_session, user.id)
        assert [r.purpose for r in rows] == [PURPOSE_RESET]
        assert rows[0].used_at is None

    async def test_the_token_in_the_email_redeems_a_working_password(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # A token that exists but cannot be redeemed is still a dead end, so
        # the link is followed all the way to a usable credential.
        user = await google_only_user(service)
        await service.request_password_set(user_id=user.id)
        token = TOKEN_IN_BODY.search(sender.sent[0][2]).group(1)

        await service.confirm_password_reset(token=token, password="brand new secret")

        refreshed = await service.user_repo.get_by_id(user.id)
        assert refreshed.password_hash is not None
        assert verify_password("brand new secret", refreshed.password_hash)

    async def test_the_link_works_over_a_real_request(
        self, auth_client, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # Same flow through HTTP, with the real session cookie, so a CSRF or
        # dependency mistake shows up here rather than in production.
        user = await google_only_user(service, email="http@example.com")
        token_row = await _issue_via_api(auth_client, service, sender, user)
        assert token_row is not None

    async def test_an_account_that_already_has_a_password_is_left_alone(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # Changing an existing password is the reset flow's job. Minting a
        # second token here would let a stray click invalidate a pending reset.
        user = await service.register(email="has@example.com", password="original secret")
        await service.user_repo.mark_email_verified(user.id)
        sender.sent.clear()  # drop the registration mail; only the call matters

        await service.request_password_set(user_id=user.id)

        assert sender.sent == []

    async def test_an_unverified_address_gets_nothing(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # Mail cannot prove ownership of an address whose verification mail is
        # precisely what never arrived.
        user = await service.user_repo.create(
            email="unverified@example.com", password_hash=None, google_sub="sub-2"
        )

        await service.request_password_set(user_id=user.id)

        assert sender.sent == []

    async def test_a_second_request_invalidates_the_first_link(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # Two live links for one account means the older one still works after
        # the user asked for a newer mail.
        user = await google_only_user(service)
        await service.request_password_set(user_id=user.id)
        first = TOKEN_IN_BODY.search(sender.sent[0][2]).group(1)

        await service.request_password_set(user_id=user.id)
        second = TOKEN_IN_BODY.search(sender.sent[1][2]).group(1)

        with pytest.raises(InvalidTokenError):
            await service.confirm_password_reset(token=first, password="stale link secret")

        await service.confirm_password_reset(token=second, password="fresh link secret")
        refreshed = await service.user_repo.get_by_id(user.id)
        assert verify_password("fresh link secret", refreshed.password_hash)

    async def test_the_email_says_it_is_a_first_password_not_a_reset(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # The user has never had a password, so "reset" reads as a threat that
        # somebody is trying to take the account over.
        user = await google_only_user(service)
        await service.request_password_set(user_id=user.id)
        _, subject, body = sender.sent[0]

        assert "google" in body.lower()
        assert "no action is needed" in body.lower()
        assert "reset your qfza password" not in subject.lower()

    async def test_setting_a_password_revokes_every_session(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        # Whoever triggered the link must not keep riding the old session.
        user = await google_only_user(service)
        issued = await service._open_session(user, ip=None, user_agent="test")
        await service.request_password_set(user_id=user.id)
        token = TOKEN_IN_BODY.search(sender.sent[0][2]).group(1)

        await service.confirm_password_reset(token=token, password="another secret")

        assert (
            await service.session_repo.get_valid(
                hash_token(issued.token), idle_ttl=service.settings.session_idle_ttl
            )
            is None
        )

    async def test_an_unknown_user_id_raises_rather_than_silently_mailing(
        self, service: AuthService, sender: FakeEmailSender
    ) -> None:
        import uuid

        with pytest.raises(AuthenticationFailedError):
            await service.request_password_set(user_id=uuid.uuid4())
        assert sender.sent == []


async def _issue_via_api(client, service: AuthService, sender: FakeEmailSender, user):  # noqa: ANN001
    """Drive the new route end to end using a real session cookie."""
    session = await service._open_session(user, ip=None, user_agent="test")
    client.cookies.set("jumpto_session", session.token)

    response = await client.post(
        "/api/v1/auth/password-set",
        headers={"X-CSRF-Token": "csrf-secret-for-tests"},
    )
    assert response.status_code == 202, response.text
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == user.email

    token = TOKEN_IN_BODY.search(sender.sent[0][2]).group(1)
    confirm = await client.post(
        "/api/v1/auth/password-reset/confirm",
        json={"token": token, "password": "set over http"},
    )
    assert confirm.status_code == 204, confirm.text
    return token


class TestCsrfHeaderIsSatisfiable:
    """The guard has to be satisfiable, or every mutation is unreachable.

    An unset secret previously made this route impossible: the client had no
    token to send and the guard rejected a missing header, so a signed-in
    visitor could not request a link and could not sign out either.
    """

    @pytest_asyncio.fixture
    async def account(self, service: AuthService):
        return await google_only_user(service, email="csrf@example.com")

    async def test_the_configured_token_is_accepted(self, auth_client, service, sender, account):
        await _issue_via_api(auth_client, service, sender, account)

    async def test_a_missing_header_is_rejected(self, auth_client, service, account):
        session = await service._open_session(account, ip=None, user_agent="test")
        auth_client.cookies.set("jumpto_session", session.token)

        response = await auth_client.post("/api/v1/auth/password-set")

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"

    async def test_a_wrong_token_is_rejected(self, auth_client, service, account):
        session = await service._open_session(account, ip=None, user_agent="test")
        auth_client.cookies.set("jumpto_session", session.token)

        response = await auth_client.post(
            "/api/v1/auth/password-set",
            headers={"X-CSRF-Token": "not-the-secret"},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"
