"""Request and response schemas for the auth and history API surface.

Field lists are explicit throughout: no ``Model(**payload.model_dump())``
anywhere, so a client can never smuggle ``user_id`` or a privilege flag in.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _normalize_email(value: str) -> str:
    """Lowercase and validate an email address."""
    stripped = value.strip().lower()
    if not _EMAIL_PATTERN.fullmatch(stripped):
        raise ValueError("Email must be a valid address")
    return stripped


class ErrorCode(str, Enum):
    """Stable, machine-readable error codes.

    The client maps a code to a localized EN/AR string, so the backend never
    ships user-facing prose. Values are SCREAMING_SNAKE to match the codes
    raised by the rest of the domain errors.
    """

    INVALID_REQUEST = "INVALID_REQUEST"
    TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    EMAIL_UNVERIFIED = "EMAIL_UNVERIFIED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    CSRF_FAILED = "CSRF_FAILED"
    CONFLICT = "CONFLICT"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"


class ApiError(BaseModel):
    """Error body returned by every auth and history endpoint."""

    code: ErrorCode
    message: str = Field(default="", description="Developer-facing detail, not localized")


class ErrorResponse(BaseModel):
    """Envelope wrapping an ApiError."""

    error: ApiError


class RegisterRequest(BaseModel):
    """Request schema for POST /api/v1/auth/register."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("password")
    @classmethod
    def reject_blank_password(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Password cannot be empty or whitespace only")
        return v


class LoginRequest(BaseModel):
    """Request schema for POST /api/v1/auth/login."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)


class GoogleLoginRequest(BaseModel):
    """Request schema for POST /api/v1/auth/google."""

    model_config = ConfigDict(str_strip_whitespace=True)

    id_token: str = Field(..., min_length=20, description="Google ID token to verify")


class PasswordResetRequest(BaseModel):
    """Request schema for POST /api/v1/auth/password-reset."""

    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(..., min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        return _normalize_email(v)


class PasswordResetConfirmRequest(BaseModel):
    """Request schema for POST /api/v1/auth/password-reset/confirm."""

    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(..., min_length=20, description="Single-use reset token")
    password: str = Field(..., min_length=10, max_length=128)

    @field_validator("password")
    @classmethod
    def reject_blank_password(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Password cannot be empty or whitespace only")
        return v


class VerifyEmailRequest(BaseModel):
    """Request schema for POST /api/v1/auth/verify."""

    model_config = ConfigDict(str_strip_whitespace=True)

    token: str = Field(..., min_length=20, description="Single-use verification token")


class AuthUserResponse(BaseModel):
    """The signed-in user. Never carries a password hash."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email: str
    email_verified: bool
    created_at: datetime


class AuthResponse(BaseModel):
    """Auth result.

    ``token`` is returned so a native client can store it in its keychain and
    send ``Authorization: Bearer``. The web client ignores it and relies on the
    cookie - persisting it in localStorage would reintroduce the XSS exposure
    that the cookie exists to prevent.
    """

    user: AuthUserResponse
    token: str = Field(..., description="Opaque session token for bearer clients")
    expires_at: datetime


class SessionInfoResponse(BaseModel):
    """Who the browser is signed in as, plus the CSRF token to echo back.

    The CSRF token is not a credential: it is shared by every visitor of this
    deployment and is worthless without the HttpOnly session cookie. The web
    client fetches it here after a page load and sends it as a header on
    state-changing requests.
    """

    user: AuthUserResponse
    csrf_token: str = Field(..., description="Send as the X-CSRF-Token header")


class HistoryEntry(BaseModel):
    """One recorded search."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: str
    keyword: str
    locale: str | None = None
    source: str | None = None
    status: str
    created_at: datetime


class HistoryResponse(BaseModel):
    """Paginated history. Cursor-based, since phone histories grow for years."""

    entries: list[HistoryEntry] = Field(default_factory=list)
    next_cursor: str | None = Field(
        default=None, description="Opaque cursor for the next page; null when exhausted"
    )
