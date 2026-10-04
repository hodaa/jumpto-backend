"""Changing a password in place while signed in.

The emailed reset link was the only way to change a password at all, so the
profile page offered a button that did nothing but mail a link. This covers the
replacement, and in particular the three ways it could be quietly worse than the
link it sits beside:

* accepting a wrong current password,
* signing the caller out of the browser they are using,
* or - the one that matters most - letting a stolen session grind the current
  password against the real hash with no lockout and no hourly ceiling.

Everything runs the real service over the real database: a mocked service cannot
show whether the other sessions were actually revoked.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.exceptions import TooManyAttemptsError, WrongCurrentPasswordError
from app.main import create_app
from app.models import Session
from app.repositories.auth_token_repository import AuthTokenRepository
from app.repositories.login_attempt_repository import LoginAttemptRepository
from app.repositories.search_history_repository import SearchHistoryRepository
from app.repositories.session_repository import SessionRepository, UserRepository
from app.services.auth import AuthService
from app.services.password import verify_password
from tests.unit.test_auth import FakeEmailSender

pytestmark = pytest.mark.asyncio

OLD_PASSWORD = "the original secret"
NEW_PASSWORD = "a brand new secret"
CLIENT_IP = "203.0.113.9"
CSRF = {"X-CSRF-Token": "csrf-secret-for-tests"}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        max_login_attempts=3,
        login_ip_hourly_limit=3,
        public_site_url="https://qfza.app",
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
async def sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest_asyncio.fixture
async def service(db_session, settings, sender) -> AuthService:  # noqa: ANN001
    return AuthService(
        user_repo=UserRepository(db_session),
        session_repo=SessionRepository(db_session),
        token_repo=AuthTokenRepository(db_session),
        history_repo=SearchHistoryRepository(db_session),
        attempt_repo=LoginAttemptRepository(db_session),
        db=None,
        settings=settings,
        email_sender=sender,
    )


@pytest_asyncio.fixture
async def auth_client(db_session, auth_settings, sender):  # noqa: ANN001
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


async def signed_in_user(
    service: AuthService,
    *,
    email: str = "owner@example.com",
    password: str = OLD_PASSWORD,
    ip: str = "198.51.100.1",
):
    """A verified account holding one live session, returned as an identity."""
    user = await service.register(email=email, password=password)
    await service.user_repo.mark_email_verified(user.id)
    issued = await service.login(email=email, password=password, ip=ip, user_agent="first")
    identity = await service.resolve_session(issued.token)
    assert identity is not None
    return identity


async def sign_in_again(service: AuthService, identity) -> uuid.UUID:  # noqa: ANN001
    """A second live session for the same account, as another browser would have."""
    issued = await service.login(
        email=identity.user.email, password=OLD_PASSWORD, ip="198.51.100.4", user_agent="other"
    )
    other = await service.resolve_session(issued.token)
    assert other is not None
    return other.session_id


async def live_session_ids(db_session, user_id) -> set[uuid.UUID]:  # noqa: ANN001
    result = await db_session.execute(
        select(Session.id).where(Session.user_id == user_id, Session.revoked_at.is_(None))
    )
    return set(result.scalars())


class TestChangePassword:
    async def test_the_new_password_is_stored_and_the_old_one_stops_working(
        self, service: AuthService
    ) -> None:
        identity = await signed_in_user(service)

        await service.change_password(
            user_id=identity.user.id,
            session_id=identity.session_id,
            current_password=OLD_PASSWORD,
            new_password=NEW_PASSWORD,
        )

        refreshed = await service.user_repo.get_by_id(identity.user.id)
        assert verify_password(NEW_PASSWORD, refreshed.password_hash)
        assert not verify_password(OLD_PASSWORD, refreshed.password_hash)

    async def test_the_browser_that_made_the_change_stays_signed_in(
        self, service: AuthService, db_session
    ) -> None:
        # The whole reason this endpoint exists: signing someone out of the tab
        # they just proved they own would be worse than the email link.
        identity = await signed_in_user(service)

        await service.change_password(
            user_id=identity.user.id,
            session_id=identity.session_id,
            current_password=OLD_PASSWORD,
            new_password=NEW_PASSWORD,
        )

        assert identity.session_id in await live_session_ids(db_session, identity.user.id)

    async def test_every_other_session_is_revoked(
        self, service: AuthService, db_session
    ) -> None:
        identity = await signed_in_user(service)
        other_id = await sign_in_again(service, identity)
        assert {identity.session_id, other_id} == await live_session_ids(db_session, identity.user.id)

        await service.change_password(
            user_id=identity.user.id,
            session_id=identity.session_id,
            current_password=OLD_PASSWORD,
            new_password=NEW_PASSWORD,
        )

        assert await live_session_ids(db_session, identity.user.id) == {identity.session_id}

    async def test_a_wrong_current_password_is_refused_and_changes_nothing(
        self, service: AuthService
    ) -> None:
        identity = await signed_in_user(service)

        with pytest.raises(WrongCurrentPasswordError):
            await service.change_password(
                user_id=identity.user.id,
                session_id=identity.session_id,
                current_password="not the password at all",
                new_password=NEW_PASSWORD,
            )

        refreshed = await service.user_repo.get_by_id(identity.user.id)
        assert verify_password(OLD_PASSWORD, refreshed.password_hash)

    async def test_an_account_with_no_password_cannot_use_this(self, service: AuthService) -> None:
        # Google-only account: there is no current password to prove, so this is
        # the wrong endpoint. It must refuse rather than quietly set one.
        user = await service.user_repo.create(
            email="g@example.com", password_hash=None, google_sub="sub-1"
        )
        await service.user_repo.mark_email_verified(user.id)
        issued = await service._open_session(user, ip=CLIENT_IP, user_agent="test")
        identity = await service.resolve_session(issued.token)
        assert identity is not None

        with pytest.raises(WrongCurrentPasswordError):
            await service.change_password(
                user_id=user.id,
                session_id=identity.session_id,
                current_password="anything at all",
                new_password=NEW_PASSWORD,
            )

        refreshed = await service.user_repo.get_by_id(user.id)
        assert refreshed.password_hash is None

    async def test_guessing_the_current_password_is_throttled(self, service: AuthService) -> None:
        # Without this, one stolen session is an unthrottled oracle on the hash.
        identity = await signed_in_user(service)

        for _ in range(3):
            with pytest.raises(WrongCurrentPasswordError):
                await service.change_password(
                    user_id=identity.user.id,
                    session_id=identity.session_id,
                    current_password="guess",
                    new_password=NEW_PASSWORD,
                    ip=CLIENT_IP,
                )

        with pytest.raises(TooManyAttemptsError):
            await service.change_password(
                user_id=identity.user.id,
                session_id=identity.session_id,
                current_password=OLD_PASSWORD,  # correct, but the ceiling is already spent
                new_password=NEW_PASSWORD,
                ip=CLIENT_IP,
            )

        refreshed = await service.user_repo.get_by_id(identity.user.id)
        assert verify_password(OLD_PASSWORD, refreshed.password_hash)


class TestChangePasswordOverHttp:
    async def _signed_in_cookie(self, auth_client, service: AuthService, email: str) -> None:
        identity = await signed_in_user(service, email=email, ip="198.51.100.2")
        auth_client.cookies.set("jumpto_session", await _token_for(service, identity))

    async def test_the_route_changes_the_password(self, auth_client, service: AuthService) -> None:
        await self._signed_in_cookie(auth_client, service, "http@example.com")

        response = await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
            headers=CSRF,
        )

        assert response.status_code == 204
        user = await service.user_repo.get_by_email("http@example.com")
        assert verify_password(NEW_PASSWORD, user.password_hash)

    async def test_the_route_keeps_the_calling_browser_signed_in(
        self, auth_client, service: AuthService
    ) -> None:
        await self._signed_in_cookie(auth_client, service, "keep@example.com")

        await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
            headers=CSRF,
        )

        session = await auth_client.get("/api/v1/auth/session")
        assert session.status_code == 200, "the caller must not be signed out by its own change"

    async def test_a_wrong_current_password_is_401(self, auth_client, service: AuthService) -> None:
        await self._signed_in_cookie(auth_client, service, "wrong@example.com")

        response = await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": "nope not it", "new_password": NEW_PASSWORD},
            headers=CSRF,
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "WRONG_CURRENT_PASSWORD"

    async def test_a_short_new_password_is_422(self, auth_client, service: AuthService) -> None:
        await self._signed_in_cookie(auth_client, service, "short@example.com")

        response = await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": OLD_PASSWORD, "new_password": "ninechars"},
            headers=CSRF,
        )

        assert response.status_code == 422

    async def test_without_the_csrf_header_it_is_403(self, auth_client, service: AuthService) -> None:
        # A cross-site form post carries the session cookie automatically; the
        # CSRF header is the only thing between that and a takeover.
        await self._signed_in_cookie(auth_client, service, "csrf@example.com")

        response = await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
        )

        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"

    async def test_without_a_session_it_is_401(self, auth_client) -> None:
        response = await auth_client.post(
            "/api/v1/auth/password-change",
            json={"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD},
            headers=CSRF,
        )

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "UNAUTHENTICATED"


async def _token_for(service: AuthService, identity) -> str:  # noqa: ANN001
    """Mint a fresh session token for an already-signed-in account."""
    issued = await service._open_session(identity.user, ip="198.51.100.7", user_agent="test")
    return issued.token
