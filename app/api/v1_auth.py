"""HTTP layer for accounts, sessions, and private search history.

Controllers stay thin: parse the request, delegate to a service, map the
outcome to a status code. No business rules, no SQL, no hashing.

Everything here is ``async def`` so the request-scoped AsyncSession and the
repositories built on it are awaited directly rather than driven across a
threadpool.
"""

from __future__ import annotations

from hmac import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.identity import build_auth_service
from app.core.client_ip import FORWARDED_HEADER, resolve_client_ip
from app.core.config import Settings, get_settings
from app.core.database import get_db_session
from app.core.exceptions import (
    AuthServiceUnavailableError,
    CsrfError,
    UnauthenticatedError,
)
from app.core.logging import get_logger
from app.models import User
from app.repositories.search_history_repository import (
    DEFAULT_PAGE_SIZE,
    SearchHistoryRepository,
    encode_cursor,
)
from app.repositories.transcript_word_repository import TranscriptWordRepository
from app.repositories.video_repository import VideoRepository
from app.schemas.auth import (
    AuthResponse,
    AuthUserResponse,
    ErrorResponse,
    GoogleLoginRequest,
    HistoryEntry,
    HistoryResponse,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RegisterRequest,
    SessionInfoResponse,
    VerifyEmailRequest,
)
from app.services.auth import AuthIdentity, AuthService, HistoryService
from app.services.email import SmtpEmailSender
from app.services.google import GoogleTokenVerifier
from app.services.search import SearchService

router = APIRouter()

logger = get_logger(__name__)


def get_email_sender(settings: Settings = Depends(get_settings)) -> SmtpEmailSender:
    """Build the transactional mail sender."""
    return SmtpEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        user=settings.smtp_user,
        app_password=settings.smtp_app_password,
        from_email=settings.email_from or settings.smtp_user,
        from_name=settings.email_from_name,
        use_ssl=settings.smtp_use_ssl,
    )


def get_google_verifier(settings: Settings = Depends(get_settings)) -> GoogleTokenVerifier:
    """Build the Google ID-token verifier.

    A dependency of its own so a test can substitute one without a network call.
    """
    return GoogleTokenVerifier(client_id=settings.google_client_id)


async def get_auth_service(
    db: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
    email_sender: SmtpEmailSender = Depends(get_email_sender),
    google_verifier: GoogleTokenVerifier = Depends(get_google_verifier),
) -> AuthService:
    """Build the auth service for this request."""
    service = build_auth_service(db=db, settings=settings)
    # Both collaborators stay injectable: a test can capture messages instead of
    # opening an SMTP connection, and can verify a token without reaching Google.
    service.email_sender = email_sender
    service.google_verifier = google_verifier
    return service


async def get_history_service(
    db: AsyncSession = Depends(get_db_session),
) -> HistoryService:
    """Build the history service for this request."""
    return HistoryService(
        history_repo=SearchHistoryRepository(db),
        video_repo=VideoRepository(db),
        search_service=SearchService(TranscriptWordRepository(db)),
    )


async def get_optional_identity(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    authorization: Annotated[str | None, Header()] = None,
) -> AuthIdentity | None:
    """Resolve the caller when a session is present, otherwise return None.

    Used by anonymous-friendly endpoints: a logged-in visitor gets their search
    recorded, an anonymous one simply proceeds. Never raises, so it cannot turn
    a public endpoint into a 401.
    """
    token = _extract_token(request, _cookie_token(request, settings), authorization)
    if not token:
        return None
    try:
        return await auth_service.resolve_session(token)
    except AuthServiceUnavailableError:
        # A search must still work when the session store is unreachable.
        logger.warning("Session lookup failed on an anonymous-friendly route")
        return None


async def get_current_identity(
    request: Request,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
    authorization: Annotated[str | None, Header()] = None,
) -> AuthIdentity:
    """Resolve the caller from a session cookie or a bearer token.

    This is the only place a user identity enters a request. Nothing downstream
    ever reads a user id from the body or the query string.
    """
    token = _extract_token(request, _cookie_token(request, settings), authorization)
    if not token:
        raise UnauthenticatedError()
    identity = await auth_service.resolve_session(token)
    if identity is None:
        raise UnauthenticatedError()
    return identity


def _cookie_token(request: Request, settings: Settings) -> str | None:
    """Read the session cookie by its configured name.

    Taken from the request rather than a declared parameter: FastAPI resolves a
    parameter's cookie name at import time, which would pin it to whatever the
    name was when the module loaded and ignore the setting entirely.
    """
    return request.cookies.get(settings.session_cookie_name)


def _bearer_token(header: str | None) -> str | None:
    """Return the token from an Authorization header, if present."""
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _extract_token(
    request: Request, cookie_token: str | None, authorization: str | None
) -> str | None:
    """Prefer the cookie, then an explicit Authorization header."""
    if cookie_token:
        return cookie_token
    return _bearer_token(request.headers.get("Authorization") or authorization)


def require_csrf(
    request: Request,
    identity: AuthIdentity = Depends(get_current_identity),
    settings: Settings = Depends(get_settings),
) -> AuthIdentity:
    """Require the CSRF header on any mutation authenticated by cookie.

    A cross-site form post carries the session cookie automatically but cannot
    set a custom header, so a state-changing cookie request must echo the token.
    A bearer-token request is exempt: it is not attached by the browser, so it
    is not forgeable from another origin in the first place.
    """
    if _bearer_token(request.headers.get("Authorization")):
        return identity
    sent = request.headers.get(settings.csrf_header_name)
    if not sent:
        raise CsrfError()
    if not compare_digest(sent, settings.csrf_token):
        raise CsrfError()
    return identity


def _set_session_cookie(response: Response, *, token: str, settings: Settings) -> None:
    """Attach the session cookie with hardening attributes.

    The cookie name carries the ``__Host-`` prefix, which makes the browser
    enforce Secure, Path=/, and the absence of a Domain attribute.
    """
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_absolute_ttl_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.cookie_is_secure,
        samesite="lax",
        path="/",
    )


def _to_user_response(user: User) -> AuthUserResponse:
    return AuthUserResponse(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        has_password=user.password_hash is not None,
        full_name=user.full_name,
    )


def _client_ip(request: Request, settings: Settings) -> str | None:
    """The address to key the per-IP throttle and session display on.

    A forwarded header is believed only when the connection came from a
    configured proxy, so a direct client cannot mint itself a fresh limiter
    bucket by inventing an X-Forwarded-For.
    """
    return resolve_client_ip(
        peer=request.client.host if request.client else None,
        forwarded_for=request.headers.get(FORWARDED_HEADER),
        trusted_networks=settings.trusted_proxy_list,
    )


# ── registration and login ─────────────────────────────────────────


@router.post(
    "/api/v1/auth/register",
    response_model=AuthUserResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse, "description": "Address already registered"}},
)
async def register(
    payload: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthUserResponse:
    """Create an account and email a verification link."""
    user = await auth_service.register(email=payload.email, password=payload.password)
    return _to_user_response(user)


@router.post(
    "/api/v1/auth/login",
    response_model=AuthResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Unknown address or wrong password"},
        423: {"model": ErrorResponse, "description": "Account locked; contact support"},
    },
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Authenticate and open a session.

    The token is returned in the body for native clients and set as a cookie
    for the browser. The web client ignores the body value: storing it in
    localStorage would reintroduce the XSS exposure the cookie avoids.
    """
    issued = await auth_service.login(
        email=payload.email,
        password=payload.password,
        ip=_client_ip(request, settings),
        user_agent=request.headers.get("User-Agent"),
    )
    _set_session_cookie(response, token=issued.token, settings=settings)
    return AuthResponse(
        user=_to_user_response(issued.user),
        token=issued.token,
        expires_at=issued.expires_at,
    )


@router.post(
    "/api/v1/auth/google",
    response_model=AuthResponse,
    responses={
        401: {"model": ErrorResponse, "description": "ID token rejected"},
        403: {"model": ErrorResponse, "description": "Address registered but unverified"},
    },
)
async def login_with_google(
    payload: GoogleLoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthResponse:
    """Sign in with a Google ID token.

    The web client obtains the token from Google's own button and sends it here.
    No client secret and no exchange call: the signature is what proves the
    address, and it is checked against Google's published keys.
    """
    issued = await auth_service.login_with_google(
        id_token=payload.id_token,
        ip=_client_ip(request, settings),
        user_agent=request.headers.get("User-Agent"),
    )
    _set_session_cookie(response, token=issued.token, settings=settings)
    return AuthResponse(
        user=_to_user_response(issued.user),
        token=issued.token,
        expires_at=issued.expires_at,
    )


@router.post(
    "/api/v1/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={401: {"model": ErrorResponse, "description": "No valid session"}},
)
async def logout(
    response: Response,
    identity: AuthIdentity = Depends(require_csrf),
    auth_service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> None:
    """Revoke the current session and clear the cookie."""
    await auth_service.logout(identity.session_id)
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_is_secure,
    )


@router.get(
    "/api/v1/auth/session",
    response_model=SessionInfoResponse,
    responses={401: {"model": ErrorResponse, "description": "No valid session"}},
)
async def current_session(
    identity: AuthIdentity = Depends(get_current_identity),
    settings: Settings = Depends(get_settings),
) -> SessionInfoResponse:
    """Report the signed-in user and hand back the CSRF token.

    Safe and cheap enough to call on every page load: the browser rehydrates its
    session state from the cookie without the token ever entering JS storage.
    """
    return SessionInfoResponse(
        user=_to_user_response(identity.user),
        csrf_token=settings.csrf_token,
    )


# ── email verification and password recovery ───────────────────────


@router.post(
    "/api/v1/auth/verify",
    response_model=AuthUserResponse,
    responses={422: {"model": ErrorResponse, "description": "Link invalid or expired"}},
)
async def verify_email(
    payload: VerifyEmailRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> AuthUserResponse:
    """Redeem an email verification link."""
    user = await auth_service.verify_email(token=payload.token)
    return _to_user_response(user)


@router.post("/api/v1/auth/password-reset", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    payload: PasswordResetRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Email a reset link.

    Always 202 whether or not the address exists, so this cannot be used to
    discover who holds an account.
    """
    await auth_service.request_password_reset(email=payload.email)


@router.post(
    "/api/v1/auth/password-set",
    status_code=status.HTTP_202_ACCEPTED,
    responses={401: {"model": ErrorResponse, "description": "No valid session"}},
)
async def request_password_set(
    identity: AuthIdentity = Depends(require_csrf),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Email the caller a link to choose a first password.

    Takes no address in the body: the recipient is the session's own account,
    so this cannot be turned into a way to mail a link to someone else.

    Separate from ``/password-reset`` because that endpoint deliberately refuses
    accounts with no password, which is exactly who needs this one.
    """
    await auth_service.request_password_set(user_id=identity.user.id)


@router.post(
    "/api/v1/auth/password-reset/confirm",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={422: {"model": ErrorResponse, "description": "Link invalid or expired"}},
)
async def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Set a new password and revoke every existing session.

    Clearing the lock here is what makes a forgotten password recoverable
    despite the permanent lockout.
    """
    await auth_service.confirm_password_reset(token=payload.token, password=payload.password)


@router.post(
    "/api/v1/auth/password-change",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={
        401: {"model": ErrorResponse, "description": "No valid session or wrong current password"},
        403: {"model": ErrorResponse, "description": "CSRF token missing or incorrect"},
    },
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    identity: AuthIdentity = Depends(require_csrf),
    settings: Settings = Depends(get_settings),
    auth_service: AuthService = Depends(get_auth_service),
) -> None:
    """Change the signed-in account's password without leaving the page.

    Requires the current password even though the caller already holds a
    session, and leaves this browser signed in while revoking the rest - the
    emailed link at ``/password-reset`` remains the route for someone who has
    forgotten the old one.
    """
    await auth_service.change_password(
        user_id=identity.user.id,
        session_id=identity.session_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip=_client_ip(request, settings),
    )


# ── private history ────────────────────────────────────────────────


@router.get(
    "/api/v1/history",
    response_model=HistoryResponse,
    responses={401: {"model": ErrorResponse, "description": "No valid session"}},
)
async def list_history(
    identity: AuthIdentity = Depends(get_current_identity),
    history_service: HistoryService = Depends(get_history_service),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=100),
    cursor: str | None = Query(default=None),
) -> HistoryResponse:
    """Return one page of the caller's own history, newest first."""
    entries = await history_service.list_page(user_id=identity.user.id, limit=limit, cursor=cursor)
    has_more = len(entries) == limit
    return HistoryResponse(
        entries=[HistoryEntry.model_validate(entry) for entry in entries],
        next_cursor=encode_cursor(entries[-1]) if has_more and entries else None,
    )


@router.delete(
    "/api/v1/history",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={401: {"model": ErrorResponse, "description": "No valid session"}},
)
async def clear_history(
    identity: AuthIdentity = Depends(require_csrf),
    history_service: HistoryService = Depends(get_history_service),
) -> None:
    """Delete the caller's entire history."""
    await history_service.clear_all(user_id=identity.user.id)


@router.delete(
    "/api/v1/history/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={
        401: {"model": ErrorResponse, "description": "No valid session"},
        404: {"model": ErrorResponse, "description": "No such entry for this user"},
    },
)
async def delete_history_entry(
    entry_id: UUID,
    identity: AuthIdentity = Depends(require_csrf),
    history_service: HistoryService = Depends(get_history_service),
) -> None:
    """Delete one of the caller's own entries.

    Scoped by user id, so another user's entry id yields 404 rather than
    revealing that it exists.
    """
    removed = await history_service.delete_entry(user_id=identity.user.id, entry_id=entry_id)
    if not removed:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="History entry not found"
        )
