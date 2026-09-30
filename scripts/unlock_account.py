#!/usr/bin/env python
"""Operator tool for unlocking an account locked out by failed logins.

Three wrong passwords lock an account for good. That is the point: the lockout
has to survive the attacker giving up. But it also strands people who mistyped
three times, and the only cure should be a human on the other end - never a
time window the attacker could simply wait out.

So unlocking is deliberately a command an operator runs against the database,
not an endpoint. There is no HTTP route here to find or call by accident.

Usage:
    python -m scripts.unlock_account someone@example.com
    python -m scripts.unlock_account someone@example.com --reason "verified by phone"

Every run prints the reason and logs it, so the action leaves a trail even
though it lives outside the request log.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session_factory
from app.core.logging import get_logger
from app.repositories.session_repository import SessionRepository, UserRepository

logger = get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def unlock(
    email: str,
    *,
    reason: str,
    revoke_sessions: bool,
    session_factory: SessionFactory | None = None,
) -> bool:
    """Unlock one account. Returns False when there was nothing to unlock.

    ``session_factory`` exists so a test can point this at its own transaction;
    the default is the application's configured database.
    """
    factory = session_factory or async_session_factory
    async with factory() as db:
        users = UserRepository(db)
        sessions = SessionRepository(db)
        if not await users.unlock(email):
            await db.rollback()
            return False

        revoked = False
        if revoke_sessions:
            # A lockout means someone was guessing the password, so any session
            # that survived must not be assumed to belong to the real owner.
            user = await users.get_by_email(email)
            if user is not None:
                await sessions.revoke_all_for_user(user.id)
                revoked = True

        await db.commit()

    logger.info(
        "Operator unlocked an account",
        email=email.lower(),
        reason=reason,
        sessions_revoked=revoked,
    )
    print(f"unlocked {email.lower()} (reason: {reason})")
    if revoked:
        print("revoked that account's existing sessions")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="the account address to unlock")
    parser.add_argument(
        "--reason",
        default="no reason given",
        help="why the lock is being cleared; recorded in the output and the log",
    )
    parser.add_argument(
        "--keep-sessions",
        action="store_true",
        help="keep existing sessions instead of revoking them",
    )
    args = parser.parse_args(argv)

    unlocked = asyncio.run(
        unlock(args.email, reason=args.reason, revoke_sessions=not args.keep_sessions)
    )
    if not unlocked:
        print(f"no locked account found for {args.email.lower()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
