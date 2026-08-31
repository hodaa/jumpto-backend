"""Pytest configuration and fixtures."""

from collections.abc import AsyncGenerator
from datetime import UTC

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import Base, get_db_session
from app.main import create_app
from app.models import TranscriptWord, Video

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
    """Disable Celery dispatch so API tests stay hermetic."""
    from app.tasks.transcription import download_and_transcribe

    monkeypatch.setattr(download_and_transcribe, "delay", lambda *args, **kwargs: None)


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


@pytest.fixture
def sample_transcript_words() -> list:
    """Sample transcript words for testing."""
    return [
        {"word_index": 0, "word": "Never", "start_time": 0.0, "end_time": 0.5},
        {"word_index": 1, "word": "gonna", "start_time": 0.5, "end_time": 1.0},
        {"word_index": 2, "word": "give", "start_time": 1.0, "end_time": 1.3},
        {"word_index": 3, "word": "you", "start_time": 1.3, "end_time": 1.6},
        {"word_index": 4, "word": "up", "start_time": 1.6, "end_time": 2.0},
        {"word_index": 5, "word": "never", "start_time": 2.0, "end_time": 2.5},
        {"word_index": 6, "word": "gonna", "start_time": 2.5, "end_time": 3.0},
        {"word_index": 7, "word": "let", "start_time": 3.0, "end_time": 3.3},
        {"word_index": 8, "word": "you", "start_time": 3.3, "end_time": 3.6},
        {"word_index": 9, "word": "down", "start_time": 3.6, "end_time": 4.0},
    ]


@pytest_asyncio.fixture
async def seeded_video(db_session: AsyncSession, sample_video_data: dict) -> Video:
    """Create a seeded video in the database."""
    video = Video(**sample_video_data)
    db_session.add(video)
    await db_session.flush()
    await db_session.refresh(video)
    return video


@pytest_asyncio.fixture
async def transcribed_video(
    db_session: AsyncSession,
    seeded_video: Video,
    sample_transcript_words: list,
) -> Video:
    """Create a video with transcript data."""
    # Add transcript words
    for word_data in sample_transcript_words:
        word = TranscriptWord(
            video_id=seeded_video.id,
            **word_data,
        )
        db_session.add(word)

    # Update video with transcript
    from datetime import datetime

    seeded_video.transcript = "Never gonna give you up never gonna let you down"
    seeded_video.transcript_tsvector = (
        "'never':1,5 'gonna':2,6 'give':3 'you':4,8 'up':5 'let':7 'down':10"
    )
    seeded_video.transcribed_at = datetime.now(UTC)

    await db_session.flush()
    await db_session.refresh(seeded_video)
    return seeded_video
