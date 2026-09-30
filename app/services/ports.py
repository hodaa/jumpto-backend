"""Protocols that services depend on, rather than concrete classes.

Only abstractions with a real second implementation live here. `SessionStore`
and the Google token verifier are intentionally absent: one implementation and
no foreseeable swap, so a protocol would be speculative.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict


class AuthEmail(BaseModel):
    """A single transactional message to send."""

    model_config = ConfigDict(frozen=True)

    to: str
    subject: str
    body: str


class PasswordHasher(Protocol):
    """Hashes and verifies passwords.

    Abstracted so tests can use a fast stand-in instead of paying the ~100ms
    that Argon2id deliberately costs.
    """

    def hash(self, password: str) -> str:
        """Return an encoded hash for a plaintext password."""
        ...

    def verify(self, password: str, encoded_hash: str) -> bool:
        """Return True when the password matches the encoded hash."""
        ...


class EmailSender(Protocol):
    """Sends transactional mail.

    Abstracted so tests can assert a message was produced without sending it.
    """

    async def send(self, email: AuthEmail) -> None:
        """Deliver a single transactional message."""
        ...
