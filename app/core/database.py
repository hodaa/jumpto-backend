"""Database configuration and session management."""

import asyncio
import ssl
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
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

settings = get_settings()

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


# NullPool: every session gets a fresh connection tied to the current event
# loop. This keeps forked Celery worker processes (which inherit the engine
# from the API parent) free from cross-loop connection bugs.
engine: AsyncEngine = _build_engine(
    settings.database_url,
    settings.is_development,
)

# Create async session factory
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a database session for dependency injection, committing on success."""
    async with async_session_factory() as session:
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
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """Apply pending database migrations via Alembic."""
    logger.info("Applying database migrations")
    try:
        await asyncio.to_thread(_run_alembic_upgrade)
        logger.info("Database migrations applied successfully")
    except Exception as e:
        logger.error("Failed to apply database migrations", error=str(e))
        raise


async def close_db() -> None:
    """Close database connections."""
    logger.info("Closing database connections")
    await engine.dispose()


def _run_alembic_upgrade() -> None:
    """Run Alembic migrations to head in a blocking worker thread."""
    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(config, "head")
