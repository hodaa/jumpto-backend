"""Verification of Google ID tokens.

The web client sends the ID token that Google already signed when the user
pressed the button; the backend checks that signature rather than exchanging
anything, so there is no client secret on this side and no round trip to Google
on sign-in beyond fetching the public keys.

What is checked, and why each matters:

- the RS256 signature against Google's published keys, so the claims really are
  Google's
- ``aud`` is this deployment's client id, so a token minted for some other app
  is refused even when it names the same person
- ``iss`` is Google, so a token from another issuer is refused
- ``exp``, enforced by the library
- ``email_verified`` is true, because an unverified address proves nothing

Keys are cached in memory for an hour. Refetching on every sign-in would make
Google a hard dependency of every login.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import jwt
from jwt import PyJWKClient

from app.core.logging import get_logger

logger = get_logger(__name__)

JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")
KEY_CACHE_SECONDS = 3600
REQUIRED_ALGORITHM = "RS256"


@dataclass(frozen=True)
class GoogleIdentity:
    """The verified claims this deployment is willing to act on."""

    subject: str
    email: str


class GoogleTokenVerifier:
    """Verifies a Google ID token against one client id."""

    def __init__(self, client_id: str) -> None:
        self.client_id = client_id
        self._client: PyJWKClient | None = None
        self._fetched_at: float = 0.0

    def _jwk_client(self) -> PyJWKClient:
        """Return the JWKS client, re-fetching the keys once an hour."""
        now = time.monotonic()
        if self._client is None or now - self._fetched_at > KEY_CACHE_SECONDS:
            self._client = PyJWKClient(JWKS_URL, cache_keys=True)
            self._fetched_at = now
            logger.info("Refetched Google signing keys")
        return self._client

    async def verify_async(self, token: str) -> GoogleIdentity | None:
        """Verify a token without blocking the event loop on the key fetch."""
        if not self.client_id:
            logger.error("Google sign-in is not configured; refusing the token")
            return None
        return await asyncio.to_thread(self._verify_sync, token)

    def _verify_sync(self, token: str) -> GoogleIdentity | None:
        """Return the verified identity, or None when the token is not ours.

        Every failure returns None rather than raising: an unverifiable token is
        a refused sign-in, not a server fault, and the caller must not be able to
        tell those apart.
        """
        try:
            signing_key = self._jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[REQUIRED_ALGORITHM],
                audience=self.client_id,
                issuer=list(GOOGLE_ISSUERS),
                options={
                    "require": ["exp", "iat", "aud", "iss", "sub", "email"],
                    # A token minted for another app is refused by the audience
                    # check; do not also refuse one Google signed but did not
                    # stamp with an iat.
                    "verify_signature": True,
                },
            )
        except jwt.PyJWTError as exc:
            # Bad signature, wrong audience, wrong issuer, expired, malformed.
            logger.info("Rejected a Google ID token", error=type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001 - any fetch failure is a refusal
            logger.warning("Could not verify a Google ID token", error=str(exc))
            return None

        email = claims.get("email")
        subject = claims.get("sub")
        # email_verified arrives as True or the string "true" depending on the
        # token, so compare loosely and refuse anything else.
        if not isinstance(email, str) or not isinstance(subject, str):
            logger.info("Rejected a Google ID token with no email or subject")
            return None
        if str(claims.get("email_verified")).lower() != "true":
            logger.info("Rejected a Google account whose email is not verified")
            return None

        return GoogleIdentity(subject=subject, email=email.lower())

    def verify(self, token: str) -> GoogleIdentity | None:
        """Synchronous verify, for callers outside the event loop."""
        if not self.client_id:
            logger.error("Google sign-in is not configured; refusing the token")
            return None
        return self._verify_sync(token)
