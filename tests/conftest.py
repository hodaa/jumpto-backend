"""Pytest configuration and fixtures."""

import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

# Force a deterministic, offline-friendly environment before app settings load.
# Live transcription now lives in the standalone worker repo, so the backend
# test suite must not depend on the ambient .env for tests that assert the
# fake/offline corpus.
os.environ["JUMPTO_LIVE_EXTERNAL_CALLS"] = "false"
os.environ["JUMPTO_TRANSCRIPT_MODE"] = "fake"

from app.core.config import get_settings  # noqa: E402
from app.core.database import Base, get_db_session  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Video  # noqa: E402

settings = get_settings()

# Use test database
TEST_DATABASE_URL = settings.database_url.replace("/jumpto", "/jumpto_test").replace(
    "postgresql://", "postgresql+asyncpg://"
)

test_engine = create_async_engine(TEST_DATABASE_URL, echo=False, poolclass=NullPool)
TestAsyncSessionFactory = async_sessionmaker(
    test_engine,
    expire_on_commit=False,
    autoflush=False,
)


@pytest_asyncio.fixture(scope="session")
async def test_db_engine():
    """Create a clean test database schema for the session."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield test_engine
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a database session for each test."""
    async with TestAsyncSessionFactory() as session:
        try:
            yield session
        finally:
            await session.close()


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Create test client with overridden database session."""

    async def override_get_db():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client_per_request() -> AsyncGenerator[AsyncClient, None]:
    """Test client that commits each request using its own DB session.

    Use for tests that exercise concurrent requests or code paths that
    run in a separate session (e.g. the transcription pipeline).
    """

    async def override_get_db():
        async with TestAsyncSessionFactory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()
            finally:
                await session.close()

    app = create_app()
    app.dependency_overrides[get_db_session] = override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _noop_task_dispatch(monkeypatch) -> None:
    """Disable Job dispatch so API tests stay hermetic."""
    from app.services.messaging import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: None)


@pytest.fixture
def sample_video_data() -> dict:
    """Sample video data for tests."""
    return {
        "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "video_id": "dQw4w9WgXcQ",
        "title": "Rick Astley - Never Gonna Give You Up",
        "duration_seconds": 212,
        "language": "en",
    }


@pytest_asyncio.fixture
async def seeded_video(db_session: AsyncSession, sample_video_data: dict) -> Video:
    """Create a seeded video in the database."""
    video = Video(**sample_video_data)
    db_session.add(video)
    await db_session.flush()
    await db_session.refresh(video)
    return video
