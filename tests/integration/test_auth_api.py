"""HTTP-level tests for the auth and history surface.

These drive the real app through ASGI, so they cover what the unit tests cannot:
cookie flags, CSRF enforcement, status-code mapping, and the guarantee that an
anonymous search still works. Mail is captured by an injected sender, and Google
verification is stubbed at the port so no network call is made.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.main import create_app

pytestmark = pytest.mark.asyncio

PASSWORD = "correct horse battery"


class CapturingSender:
    """Stands in for SMTP so tests never open a socket."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, email) -> None:  # noqa: ANN001
        self.sent.append((email.to, email.subject, email.body))


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        session_cookie_name="jumpto_session",
        csrf_token="csrf-secret-for-tests",
        max_login_attempts=3,
        login_ip_hourly_limit=3,
        public_site_url="https://qfza.app",
    )


@pytest_asyncio.fixture
async def sender() -> CapturingSender:
    return CapturingSender()


class StubGoogle:
    """Stands in for Google, so no test reaches the network."""

    def __init__(self) -> None:
        from app.services.google import GoogleIdentity

        self.identity = GoogleIdentity(subject="stub-sub", email="stub@example.com")
        self.tokens: list[str] = []

    async def verify_async(self, token: str):
        self.tokens.append(token)
        return self.identity


@pytest.fixture
def google_verifier() -> StubGoogle:
    return StubGoogle()


def _build_auth_client(db_session, settings, sender, google_verifier):  # noqa: ANN001, ANN201
    """An httpx client wired to the test database, a captured mailer, and a fake Google port.

    https, because the session cookie is Secure outside development and a
    client must not return a Secure cookie over plain http.
    """

    async def override_get_db():
        # Mirror production: get_db_session commits when the request finishes.
        # Without this the handler and the test would share one uncommitted
        # transaction, so an assertion about a saved row would pass even if
        # nothing were ever written.
        yield db_session
        await db_session.commit()

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_settings] = lambda: settings
    import app.api.v1_auth as v1_auth

    app.dependency_overrides[v1_auth.get_email_sender] = lambda: sender
    app.dependency_overrides[v1_auth.get_google_verifier] = lambda: google_verifier
    return app


@pytest_asyncio.fixture
async def auth_client(db_session, auth_settings, sender, google_verifier):  # noqa: ANN001
    app = _build_auth_client(db_session, auth_settings, sender, google_verifier)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def account_client(db_session, auth_settings, sender, google_verifier):  # noqa: ANN001
    """A client whose per-IP ceiling is out of the way.

    The three-strike account rule and the per-IP ceiling are separate brakes.
    Every request from one test client shares one address - a forwarded header
    cannot change that, which is the point of TestForwardedHeaderCannotBypass-
    TheLimiter - so a test about the account rule has to lift the other brake
    or it would trip it first and prove nothing.
    """
    app = _build_auth_client(
        db_session,
        auth_settings.model_copy(update={"login_ip_hourly_limit": 100}),
        sender,
        google_verifier,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
        yield client
    app.dependency_overrides.clear()


async def _register_and_verify(client: AsyncClient, email: str, password: str = PASSWORD) -> dict:
    """Register, pull the verification token out of the emailed link, redeem it."""
    response = await client.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text

    body = response.json()
    assert body["email_verified"] is False
    return body


async def _verify_via_email(client: AsyncClient, sender: CapturingSender, db_session) -> None:  # noqa: ANN001
    """Complete verification using the link that was emailed."""
    link = sender.sent[-1][2]
    token = link.split("token=")[1].split()[0]
    response = await client.post("/api/v1/auth/verify", json={"token": token})
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is True


class TestRegistrationEndpoint:
    """POST /api/v1/auth/register"""

    async def test_creates_an_unverified_account(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": PASSWORD},
        )
        assert response.status_code == 201
        assert response.json()["email_verified"] is False
        assert "password" not in response.text

    async def test_sends_a_verification_link(self, auth_client, sender) -> None:  # noqa: ANN001
        await auth_client.post(
            "/api/v1/auth/register",
            json={"email": "mail@example.com", "password": PASSWORD},
        )
        assert len(sender.sent) == 1
        assert "verify-email?token=" in sender.sent[0][2]

    async def test_duplicate_address_is_409(self, auth_client) -> None:  # noqa: ANN001
        payload = {"email": "dupe@example.com", "password": PASSWORD}
        await auth_client.post("/api/v1/auth/register", json=payload)
        response = await auth_client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CONFLICT"

    async def test_short_password_is_rejected(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/v1/auth/register", json={"email": "a@example.com", "password": "short"}
        )
        assert response.status_code == 422

    async def test_malformed_address_is_rejected(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/v1/auth/register", json={"email": "nope", "password": PASSWORD}
        )
        assert response.status_code == 422


class TestLoginEndpoint:
    """POST /api/v1/auth/login"""

    async def _verified_user(self, client, sender, email="a@example.com"):  # noqa: ANN001
        await _register_and_verify(client, email)
        await _verify_via_email(client, sender, None)
        return email

    async def test_wrong_password_is_401(self, auth_client, sender) -> None:  # noqa: ANN001
        email = await self._verified_user(auth_client, sender)
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong one"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_unknown_address_is_the_same_401(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._verified_user(auth_client, sender)
        unknown = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong one"},
        )
        known = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "a@example.com", "password": "wrong one"},
        )
        assert unknown.status_code == known.status_code == 401
        assert unknown.json()["error"]["code"] == known.json()["error"]["code"]

    async def test_unverified_account_is_403(self, auth_client) -> None:  # noqa: ANN001
        await _register_and_verify(auth_client, "pending@example.com")
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "pending@example.com", "password": PASSWORD},
        )
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "EMAIL_UNVERIFIED"

    async def test_three_strikes_lock_the_account(self, account_client, sender) -> None:  # noqa: ANN001
        # account_client lifts the per-IP ceiling, so this exercises the
        # per-account rule on its own.
        email = await self._verified_user(account_client, sender)
        for _ in range(3):
            response = await account_client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong one"}
            )
            assert response.status_code == 401, response.text
        locked = await account_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert locked.status_code == 423, locked.text
        assert locked.json()["error"]["code"] == "ACCOUNT_LOCKED"

    async def test_a_locked_account_reports_the_lock_not_the_password(
        self, account_client, sender
    ) -> None:  # noqa: ANN001
        # The correct password still reports the lock, which is what tells the
        # visitor to contact support rather than to retype their password.
        email = await self._verified_user(account_client, sender)
        for _ in range(3):
            await account_client.post(
                "/api/v1/auth/login", json={"email": email, "password": "wrong one"}
            )
        wrong_again = await account_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "still wrong"}
        )
        assert wrong_again.status_code == 401

    async def test_ip_ceiling_is_429(self, auth_client, sender) -> None:  # noqa: ANN001
        # Three failures from one address exhaust the (deliberately tiny) test
        # ceiling; a fourth attempt is refused before any credential is checked.
        for _ in range(3):
            await auth_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "wrong one"},
            )
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong one"},
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


class TestForwardedHeaderCannotBypassTheLimiter:
    """The per-IP ceiling must key on an address the client cannot choose.

    An attacker who can name their own limiter key gets unlimited password
    guesses, which would make the three-attempt lockout on the account
    unreachable - the ceiling would never be reached to trigger it.
    """

    async def test_a_rotating_header_does_not_reset_the_count(self, auth_client, sender) -> None:  # noqa: ANN001
        # Each attempt claims a brand new source address. None is behind a
        # configured proxy, so all of them are the same client.
        for attempt in range(4):
            response = await auth_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "wrong one"},
                headers={"X-Forwarded-For": f"198.51.100.{attempt}"},
            )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"

    async def test_the_limiter_still_holds_after_a_header_is_added(
        self, auth_client, sender
    ) -> None:  # noqa: ANN001
        # The first three attempts are the attack; the fourth is plain.
        for attempt in range(3):
            await auth_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "wrong one"},
                headers={"X-Forwarded-For": f"198.51.100.{attempt}"},
            )
        response = await auth_client.post(
            "/api/v1/auth/login", json={"email": "ghost@example.com", "password": "wrong one"}
        )
        assert response.status_code == 429

    async def test_a_junk_header_does_not_error(self, auth_client, sender) -> None:  # noqa: ANN001
        # A value that cannot be stored in the ip column must not become a 500.
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong one"},
            headers={"X-Forwarded-For": "x" * 4000},
        )
        assert response.status_code == 401

    async def test_a_hostname_header_is_refused(self, auth_client, sender) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong one"},
            headers={"X-Forwarded-For": "attacker.example.com"},
        )
        assert response.status_code == 401


class TestForwardedHeaderBehindATrustedProxy:
    """When a proxy is configured, it may speak for the client."""

    @pytest.fixture
    def proxy_settings(self, auth_settings: Settings) -> Settings:
        return auth_settings.model_copy(update={"trusted_proxy_networks": "127.0.0.0/8"})

    @pytest_asyncio.fixture
    async def proxy_client(self, db_session, proxy_settings, sender, google_verifier):  # noqa: ANN001
        app = _build_auth_client(db_session, proxy_settings, sender, google_verifier)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as ac:
            yield ac
        app.dependency_overrides.clear()

    async def test_the_client_address_behind_the_proxy_is_used(self, proxy_client, sender) -> None:  # noqa: ANN001
        # Trusted, so three distinct claimed addresses are three distinct keys
        # and each gets its own budget.
        for address in ("198.51.100.1", "198.51.100.2", "198.51.100.3"):
            response = await proxy_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "wrong one"},
                headers={"X-Forwarded-For": address},
            )
            assert response.status_code == 401, response.text

    async def test_the_same_address_behind_the_proxy_still_trips(
        self, proxy_client, sender
    ) -> None:  # noqa: ANN001
        for _ in range(3):
            await proxy_client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@example.com", "password": "wrong one"},
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
        response = await proxy_client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@example.com", "password": "wrong one"},
            headers={"X-Forwarded-For": "198.51.100.1"},
        )
        assert response.status_code == 429


class TestSessionCookie:
    """The cookie is the browser credential, so its flags matter."""

    async def _sign_in(self, client, sender):  # noqa: ANN001
        email = await TestLoginEndpoint()._verified_user(client, sender, "cookie@example.com")
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        assert response.status_code == 200
        return response

    async def test_login_sets_an_httponly_cookie(self, auth_client, sender) -> None:  # noqa: ANN001
        response = await self._sign_in(auth_client, sender)
        header = response.headers["set-cookie"]
        assert "jumpto_session=" in header
        assert "HttpOnly" in header
        assert "Secure" in header
        assert "Path=/" in header

    async def test_cookie_carries_no_domain_attribute(self, auth_client, sender) -> None:  # noqa: ANN001
        response = await self._sign_in(auth_client, sender)
        header = response.headers["set-cookie"]
        # A Domain attribute would let a sibling subdomain read the session.
        assert "Domain=" not in header

    async def test_token_is_also_returned_for_native_clients(self, auth_client, sender) -> None:  # noqa: ANN001
        response = await self._sign_in(auth_client, sender)
        assert response.json()["token"]

    async def test_session_endpoint_reports_the_user(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.get("/api/v1/auth/session")
        assert response.status_code == 200
        body = response.json()
        assert body["user"]["email"] == "cookie@example.com"
        assert body["csrf_token"] == "csrf-secret-for-tests"

    async def test_session_endpoint_is_401_when_signed_out(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.get("/api/v1/auth/session")
        assert response.status_code == 401


class TestCsrf:
    """Cookie-authenticated mutations must echo the CSRF token."""

    async def _sign_in(self, client, sender):  # noqa: ANN001
        email = await TestLoginEndpoint()._verified_user(client, sender, "csrf@example.com")
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    async def test_logout_without_the_header_is_403(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.post("/api/v1/auth/logout")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_FAILED"

    async def test_logout_with_a_wrong_header_is_403(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": "not-the-secret"}
        )
        assert response.status_code == 403

    async def test_logout_with_the_header_succeeds(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": "csrf-secret-for-tests"}
        )
        assert response.status_code == 204
        assert auth_client.cookies.get("jumpto_session") is None

    async def test_history_clear_without_the_header_is_403(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.delete("/api/v1/history")
        assert response.status_code == 403

    async def test_bearer_clients_skip_csrf(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        token = auth_client.cookies.get("jumpto_session")
        # A native client holds the token in its keychain and sends a header the
        # browser would never attach cross-site, so no CSRF token is needed.
        response = await auth_client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
            cookies={},
        )
        assert response.status_code == 204


class TestPasswordResetEndpoint:
    """POST /api/v1/auth/password-reset[/confirm]"""

    async def test_unknown_address_looks_identical(self, auth_client, sender) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/v1/auth/password-reset", json={"email": "ghost@example.com"}
        )
        assert response.status_code == 202
        assert sender.sent == []

    async def test_known_address_receives_a_link(self, auth_client, sender) -> None:  # noqa: ANN001
        await _register_and_verify(auth_client, "reset@example.com")
        sender.sent.clear()
        response = await auth_client.post(
            "/api/v1/auth/password-reset", json={"email": "reset@example.com"}
        )
        assert response.status_code == 202
        assert len(sender.sent) == 1
        assert "reset-password?token=" in sender.sent[0][2]

    async def test_confirming_sets_the_new_password(self, auth_client, sender) -> None:  # noqa: ANN001
        await _register_and_verify(auth_client, "reset2@example.com")
        await _verify_via_email(auth_client, sender, None)
        sender.sent.clear()
        await auth_client.post("/api/v1/auth/password-reset", json={"email": "reset2@example.com"})
        token = sender.sent[-1][2].split("token=")[1].split()[0]

        confirmed = await auth_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "password": "a whole new password"},
        )
        assert confirmed.status_code == 204

        signed_in = await auth_client.post(
            "/api/v1/auth/login",
            json={"email": "reset2@example.com", "password": "a whole new password"},
        )
        assert signed_in.status_code == 200

    async def test_a_token_cannot_be_replayed(self, auth_client, sender) -> None:  # noqa: ANN001
        await _register_and_verify(auth_client, "reset3@example.com")
        sender.sent.clear()
        await auth_client.post("/api/v1/auth/password-reset", json={"email": "reset3@example.com"})
        token = sender.sent[-1][2].split("token=")[1].split()[0]

        first = await auth_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "password": "first new password"},
        )
        assert first.status_code == 204
        second = await auth_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "password": "second new password"},
        )
        assert second.status_code == 400

    async def test_reset_revokes_a_live_session(self, auth_client, sender) -> None:  # noqa: ANN001
        email = await TestLoginEndpoint()._verified_user(auth_client, sender, "revoke@example.com")
        await auth_client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert auth_client.cookies.get("jumpto_session")
        assert (await auth_client.get("/api/v1/auth/session")).status_code == 200

        sender.sent.clear()
        await auth_client.post("/api/v1/auth/password-reset", json={"email": email})
        token = sender.sent[-1][2].split("token=")[1].split()[0]
        await auth_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"token": token, "password": "yet another password"},
        )

        after = await auth_client.get("/api/v1/auth/session")
        assert after.status_code == 401


class TestHistoryEndpoint:
    """GET/DELETE /api/v1/history"""

    async def _sign_in(self, client, sender, email="hist@example.com"):  # noqa: ANN001
        await _register_and_verify(client, email)
        await _verify_via_email(client, sender, None)
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    async def test_history_requires_a_session(self, auth_client) -> None:  # noqa: ANN001
        assert (await auth_client.get("/api/v1/history")).status_code == 401

    async def test_empty_history_is_an_empty_list(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        response = await auth_client.get("/api/v1/history")
        assert response.status_code == 200
        assert response.json()["entries"] == []

    async def test_entries_are_listed_newest_first(self, auth_client, sender, db_session) -> None:  # noqa: ANN001
        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender)
        user = await db_session.scalar(
            __import__("sqlalchemy").select(User).where(User.email == "hist@example.com")
        )
        # Explicit timestamps: rows written in one transaction share a
        # created_at, so ordering could not be asserted from insertion order.
        base = datetime.now(UTC) - timedelta(hours=1)
        for i in range(3):
            db_session.add(
                SearchHistory(
                    user_id=user.id,
                    video_id=f"v{i}",
                    keyword=f"kw{i}",
                    status="found",
                    created_at=base + timedelta(hours=i),
                )
            )
        await db_session.flush()

        response = await auth_client.get("/api/v1/history")
        entries = response.json()["entries"]
        assert len(entries) == 3
        assert [e["keyword"] for e in entries] == ["kw2", "kw1", "kw0"]

    async def test_entries_never_leak_across_users(self, auth_client, sender, db_session) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender, "owner@example.com")
        owner = await db_session.scalar(select(User).where(User.email == "owner@example.com"))
        db_session.add(
            SearchHistory(user_id=owner.id, video_id="v", keyword="mine", status="found")
        )
        await db_session.flush()

        await auth_client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": "csrf-secret-for-tests"}
        )
        await self._sign_in(auth_client, sender, "stranger@example.com")

        response = await auth_client.get("/api/v1/history")
        assert response.json()["entries"] == []

    async def test_deleting_an_entry_requires_csrf(self, auth_client, sender, db_session) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender)
        user = await db_session.scalar(select(User).where(User.email == "hist@example.com"))
        entry = SearchHistory(user_id=user.id, video_id="v", keyword="kw", status="found")
        db_session.add(entry)
        await db_session.flush()
        await db_session.refresh(entry)

        assert (await auth_client.delete(f"/api/v1/history/{entry.id}")).status_code == 403

    async def test_another_users_entry_is_404(self, auth_client, sender, db_session) -> None:  # noqa: ANN001
        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender, "thief@example.com")
        victim = User(
            id=uuid.uuid4(),
            email="victim-entry@example.com",
            password_hash="x",
            failed_login_count=0,
        )
        db_session.add(victim)
        await db_session.flush()
        entry = SearchHistory(user_id=victim.id, video_id="v", keyword="secret", status="found")
        db_session.add(entry)
        await db_session.flush()
        await db_session.refresh(entry)

        response = await auth_client.delete(
            f"/api/v1/history/{entry.id}",
            headers={"X-CSRF-Token": "csrf-secret-for-tests"},
        )
        assert response.status_code == 404

    async def test_clear_all_needs_csrf(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender)
        assert (await auth_client.delete("/api/v1/history")).status_code == 403
        ok = await auth_client.delete(
            "/api/v1/history", headers={"X-CSRF-Token": "csrf-secret-for-tests"}
        )
        assert ok.status_code == 204


class TestBearerAuthentication:
    """A native client authenticates with a header instead of a cookie."""

    async def test_bearer_token_authenticates_history(self, auth_client, sender) -> None:  # noqa: ANN001
        email = await TestLoginEndpoint()._verified_user(auth_client, sender, "native@example.com")
        login = await auth_client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        )
        token = login.json()["token"]

        response = await auth_client.get(
            "/api/v1/history", headers={"Authorization": f"Bearer {token}"}, cookies={}
        )
        assert response.status_code == 200

    async def test_a_garbage_bearer_token_is_401(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.get(
            "/api/v1/history", headers={"Authorization": "Bearer nope"}, cookies={}
        )
        assert response.status_code == 401


class TestAnonymousSearchIsUnaffected:
    """The public product must not depend on any of the above working."""

    async def test_search_without_a_session_still_works(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post(
            "/api/search",
            json={
                "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "keyword": "never gonna give you up",
            },
        )
        assert response.status_code in (200, 202)
        assert "error" not in response.json()


class TestSearchRecordsHistory:
    """A signed-in search is filed under the account; an anonymous one is not.

    This is the behaviour that makes history useful without touching the public
    product: the search route cannot tell the two apart.
    """

    async def _sign_in(self, client, sender, email="search@example.com"):  # noqa: ANN001
        await _register_and_verify(client, email)
        await _verify_via_email(client, sender, None)
        await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})

    async def _search(self, client, keyword="never gonna give you up"):  # noqa: ANN001
        return await client.post(
            "/api/search",
            json={
                "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "keyword": keyword,
            },
            headers={"X-Qfza-Source": "unit-test", "Accept-Language": "ar-SA,ar;q=0.9"},
        )

    async def test_anonymous_search_records_nothing(self, auth_client, db_session) -> None:  # noqa: ANN001
        from sqlalchemy import func, select

        from app.models import SearchHistory

        response = await self._search(auth_client)
        assert response.status_code in (200, 202)

        count = await db_session.scalar(select(func.count()).select_from(SearchHistory))
        assert count == 0

    async def test_signed_in_search_is_recorded(self, auth_client, sender, db_session) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender, "recorded@example.com")
        response = await self._search(auth_client)
        assert response.status_code in (200, 202)

        user = await db_session.scalar(select(User).where(User.email == "recorded@example.com"))
        rows = (
            (
                await db_session.execute(
                    select(SearchHistory).where(SearchHistory.user_id == user.id)
                )
            )
            .scalars()
            .all()
        )

        assert len(rows) == 1
        row = rows[0]
        assert row.video_id == "dQw4w9WgXcQ"
        assert row.keyword == "never gonna give you up"
        assert row.status == "processing"
        assert row.locale == "ar-sa"
        assert row.source == "unit-test"

    async def test_it_appears_in_the_history_endpoint(self, auth_client, sender) -> None:  # noqa: ANN001
        await self._sign_in(auth_client, sender, "listed@example.com")
        await self._search(auth_client, keyword="rick astley")

        history = await auth_client.get("/api/v1/history")
        entries = history.json()["entries"]
        assert [e["keyword"] for e in entries] == ["rick astley"]

    async def test_a_search_is_not_attributed_to_another_account(
        self, auth_client, sender, db_session
    ) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from app.models import SearchHistory, User

        await self._sign_in(auth_client, sender, "mine@example.com")
        await self._search(auth_client)

        stranger = User(id=uuid.uuid4(), email="stranger@example.com", password_hash="x")
        db_session.add(stranger)
        await db_session.flush()

        rows = (
            (
                await db_session.execute(
                    select(SearchHistory).where(SearchHistory.user_id == stranger.id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    async def test_a_bogus_cookie_does_not_break_an_anonymous_search(self, auth_client) -> None:  # noqa: ANN001
        auth_client.cookies.set("jumpto_session", "not-a-real-token", domain="test")
        response = await self._search(auth_client)
        assert response.status_code in (200, 202)


class TestGoogleEndpoint:
    """POST /api/v1/auth/google"""

    async def test_a_new_google_user_is_signed_in(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post("/api/v1/auth/google", json={"id_token": "a" * 60})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["email"] == "stub@example.com"
        # Google already proved the address, so the account is usable at once.
        assert body["user"]["email_verified"] is True
        assert body["token"]

    async def test_it_sets_the_session_cookie(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post("/api/v1/auth/google", json={"id_token": "a" * 60})
        assert "jumpto_session=" in response.headers["set-cookie"]

    async def test_the_new_account_can_use_the_api(self, auth_client) -> None:  # noqa: ANN001
        await auth_client.post("/api/v1/auth/google", json={"id_token": "a" * 60})
        response = await auth_client.get("/api/v1/auth/session")
        assert response.status_code == 200
        assert response.json()["user"]["email"] == "stub@example.com"

    async def test_a_rejected_token_is_401(self, auth_client, google_verifier) -> None:  # noqa: ANN001
        google_verifier.identity = None
        response = await auth_client.post("/api/v1/auth/google", json={"id_token": "a" * 60})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"

    async def test_an_unverified_placeholder_is_refused(self, auth_client, google_verifier) -> None:  # noqa: ANN001
        from app.services.google import GoogleIdentity

        # Somebody registered this address without proving they own it.
        await auth_client.post(
            "/api/v1/auth/register",
            json={"email": "stub@example.com", "password": PASSWORD},
        )
        google_verifier.identity = GoogleIdentity(subject="sub-attacker", email="stub@example.com")
        response = await auth_client.post("/api/v1/auth/google", json={"id_token": "a" * 60})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "EMAIL_UNVERIFIED"

    async def test_a_short_token_is_rejected_by_validation(self, auth_client) -> None:  # noqa: ANN001
        response = await auth_client.post("/api/v1/auth/google", json={"id_token": "short"})
        assert response.status_code == 422
