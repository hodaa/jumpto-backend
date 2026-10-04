"""Database configuration and session management."""

import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse, urlunparse

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_SSL_MODE_MAP: dict[str, ssl.VerifyMode | bool] = {
    "disable": False,
    "allow": False,
    "prefer": False,
    "require": True,
    "verify-ca": ssl.CERT_REQUIRED,
    "verify-full": ssl.CERT_REQUIRED,
}


def _ssl_context_for_mode(sslmode: str) -> ssl.SSLContext | bool:
    """Translate a libpq sslmode to an asyncpg-compatible ssl value."""
    value = _SSL_MODE_MAP.get(sslmode, ssl.CERT_REQUIRED)
    if isinstance(value, bool):
        return value
    context = ssl.create_default_context()
    context.verify_mode = value
    return context


def build_async_database_url(database_url: str) -> tuple[str, dict[str, object]]:
    """Translate a libpq URL to an asyncpg URL plus ssl connect args."""
    parsed = urlparse(database_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    sslmode = query.pop("sslmode", ["prefer"])[0].lower()
    async_url = urlunparse(parsed._replace(query="")).replace(
        "postgresql://", "postgresql+asyncpg://"
    )

    ssl_value = _ssl_context_for_mode(sslmode)
    connect_args = {} if ssl_value is False else {"ssl": ssl_value}
    return async_url, connect_args


def _build_engine(database_url: str, echo: bool) -> AsyncEngine:
    """Build an async engine, translating sslmode to asyncpg's ssl param."""
    async_url, connect_args = build_async_database_url(database_url)

    kwargs: dict[str, object] = {"echo": echo, "poolclass": NullPool}
    if connect_args:
        kwargs["connect_args"] = connect_args
    return create_async_engine(async_url, **kwargs)


# The engine and session factory are built lazily on first use rather than
# at import time, so importing this module does not require the full
# application settings. Alembic imports ``Base`` from here, and migrations
# must be able to run without runtime secrets (such as CSRF_TOKEN) that
# they never use.
#
# NullPool: every session gets a fresh connection tied to the current event
# loop. This keeps forked Celery worker processes free from cross-loop
# connection bugs.
_engine: AsyncEngine | None = None
async_session_factory: async_sessionmaker[AsyncSession] | None = None


def _get_engine() -> AsyncEngine:
    """Return the shared engine, building it on first use."""
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = _build_engine(settings.database_url, settings.is_development)
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, building it on first use."""
    global async_session_factory
    if async_session_factory is None:
        async_session_factory = async_sessionmaker(
            _get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return async_session_factory


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session for dependency injection, committing on success."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session as a context manager, committing on success."""
    session_factory = _get_session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Initialize database connection."""
    logger.info("Initializing database connection")
    try:
        async with _get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error("Failed to initialize database", error=str(e))
        raise


async def close_db() -> None:
    """Close database connections."""
    logger.info("Closing database connections")
    await _get_engine().dispose()
