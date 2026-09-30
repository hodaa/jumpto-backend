"""Password hashing and verification.

Argon2id via pwdlib (the maintained successor to the abandoned passlib).
"""

from __future__ import annotations

from argon2 import PasswordHasher as _Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# One module-level instance: constructing it per call would rebuild the Argon2
# parameters and throw away the cost settings.
_hasher = _Argon2Hasher()


def hash_password(password: str) -> str:
    """Return an Argon2id-encoded hash for a plaintext password."""
    return _hasher.hash(password)


def verify_password(password: str, encoded_hash: str) -> bool:
    """Return True when the password matches the encoded hash.

    Never raises on a malformed or missing hash - callers treat any failure as
    "not a match" so that a corrupt row cannot turn into a 500.
    """
    if not encoded_hash:
        return False
    try:
        return _hasher.verify(encoded_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
