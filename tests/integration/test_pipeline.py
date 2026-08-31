"""Integration tests for the transcription pipeline."""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ExternalServiceError
from app.models import Job, TranscriptWord, Video
from app.providers import (
    FakeTranscriptProvider,
    MediaInfo,
    TranscriptData,
    TranscriptProvider,
    TranscriptWordData,
)
from app.tasks.transcription import (
    _EXTERNAL_FAILURE,
    _fetch_transcript_with_retry,
    run_pipeline,
)
from tests.conftest import TestAsyncSessionFactory

_FAKE_URL = "https://www.youtube.com/watch?v=pipeline123"


def _fake_media(video_id: str, youtube_url: str = "") -> MediaInfo:
    """Deterministic media info for a fake video id."""
    return MediaInfo(title=f"JumpTo test video {video_id}", duration_seconds=300)


@pytest.fixture(autouse=True)
def _force_fake_mode(monkeypatch):
    """Keep the pipeline hermetic regardless of environment .env settings."""
    monkeypatch.setattr("app.tasks.transcription.get_media_info", _fake_media)
    monkeypatch.setattr("app.tasks.transcription.get_transcript_provider", FakeTranscriptProvider)


@pytest.mark.asyncio
async def test_pipeline_fake_mode_end_to_end(client_per_request: AsyncClient) -> None:
    """
    POST /api/search queues a job; running the pipeline persists the
    transcript and a later search for the same video is a cache hit.
    """
    response = await client_per_request.post(
        "/api/search",
        json={"youtube_url": _FAKE_URL, "keyword": "quick brown fox"},
    )
    assert response.status_code == 202
    data = response.json()
    job_uuid, video_uuid = data["job_id"], data["video_id"]

    result = await run_pipeline(str(job_uuid), "pipeline123", _FAKE_URL, TestAsyncSessionFactory)
    assert result["status"] == "completed"

    async with TestAsyncSessionFactory() as session:
        job = await session.get(Job, job_uuid)
        assert job.status == "completed"
        assert job.progress == 100

        video = await session.get(Video, video_uuid)
        assert video.transcribed_at is not None
        assert video.title == "JumpTo test video pipeline123"
        assert video.duration_seconds == 300
        assert video.transcript_tsvector is not None

        words = (
            (
                await session.execute(
                    select(TranscriptWord)
                    .where(TranscriptWord.video_id == video_uuid)
                    .order_by(TranscriptWord.word_index)
                )
            )
            .scalars()
            .all()
        )
        assert len(words) > 0
        assert words[0].word == "the"
        assert words[0].start_time == 0.0

    # Re-search is a cache hit with correct snippet
    response = await client_per_request.post(
        "/api/search",
        json={"youtube_url": _FAKE_URL, "keyword": "quick brown fox"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 1
    assert results[0]["timestamp"] == "00:00"
    assert results[0]["progress_seconds"] == 0.5
    assert "quick brown fox" in results[0]["text_snippet"]


@pytest.mark.asyncio
async def test_search_dispatches_pipeline_task(client: AsyncClient, monkeypatch) -> None:
    """POST /api/search enqueues the pipeline with the created job id."""
    from app.tasks.transcription import download_and_transcribe

    dispatch_calls: list = []
    monkeypatch.setattr(
        download_and_transcribe,
        "delay",
        lambda *args, **kwargs: dispatch_calls.append(args),
    )

    response = await client.post(
        "/api/search",
        json={"youtube_url": "https://www.youtube.com/watch?v=dispatch123", "keyword": "test"},
    )
    assert response.status_code == 202
    job_id = response.json()["job_id"]

    assert len(dispatch_calls) == 1
    assert dispatch_calls[0][0] == str(job_id)
    assert dispatch_calls[0][1] == "dispatch123"


@pytest.mark.asyncio
async def test_pipeline_failure_marks_job_failed(
    client_per_request: AsyncClient, monkeypatch
) -> None:
    """A failing transcript provider marks the job failed with a user-safe error."""
    url = "https://www.youtube.com/watch?v=failpipe123"
    response = await client_per_request.post(
        "/api/search", json={"youtube_url": url, "keyword": "test"}
    )
    assert response.status_code == 202
    job_uuid = response.json()["job_id"]

    class BoomTranscriptProvider(TranscriptProvider):
        """Provider that always fails."""

        async def fetch(self, youtube_url: str) -> TranscriptData:
            raise ExternalServiceError("upstream boom", service="assemblyai")

    monkeypatch.setattr(
        "app.tasks.transcription.get_transcript_provider",
        lambda: BoomTranscriptProvider(),
    )

    with pytest.raises(ExternalServiceError):
        await run_pipeline(str(job_uuid), "failpipe123", url, TestAsyncSessionFactory)

    async with TestAsyncSessionFactory() as session:
        job = await session.get(Job, job_uuid)
        assert job.status == "failed"
        assert job.error == _EXTERNAL_FAILURE
        assert "boom" not in job.error


@pytest.mark.asyncio
async def test_pipeline_is_idempotent(client_per_request: AsyncClient) -> None:
    """Running the pipeline twice does not duplicate transcript words."""
    url = "https://www.youtube.com/watch?v=idemtest123"
    response = await client_per_request.post(
        "/api/search", json={"youtube_url": url, "keyword": "test"}
    )
    job_uuid = response.json()["job_id"]

    await run_pipeline(str(job_uuid), "idemtest123", url, TestAsyncSessionFactory)
    second = await run_pipeline(str(job_uuid), "idemtest123", url, TestAsyncSessionFactory)
    assert second["status"] == "completed"

    async with TestAsyncSessionFactory() as session:
        video = await session.execute(select(Video).where(Video.video_id == "idemtest123"))
        video = video.scalar_one()
        words = (
            (
                await session.execute(
                    select(TranscriptWord).where(TranscriptWord.video_id == video.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(words) == 28  # Fixed fake corpus, no duplicates

        # job and full-text index are intact
        job = await session.get(Job, job_uuid)
        assert job.status == "completed"


@pytest.mark.asyncio
async def test_pipeline_duplicate_words_constraint_prevents_corruption() -> None:
    """The (video_id, word_index) uniqueness guards against duplicate writes."""
    async with TestAsyncSessionFactory() as session:
        video = Video(
            youtube_url="https://www.youtube.com/watch?v=dupcon12345", video_id="dupcon12345"
        )
        session.add(video)
        await session.flush()

        session.add(
            TranscriptWord(video_id=video.id, word_index=0, word="hi", start_time=0.0, end_time=0.5)
        )
        session.add(
            TranscriptWord(
                video_id=video.id, word_index=0, word="again", start_time=1.0, end_time=1.5
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


class TestCaptionFastPath:
    """Tests for the caption-first transcript fetch fallback."""

    def _transcript(self) -> TranscriptData:
        return TranscriptData(
            language="en",
            text="hello world",
            words=[TranscriptWordData(word="hello", start_time=0.0, end_time=0.5)],
        )

    @pytest.mark.asyncio
    async def test_captions_used_first_when_live(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        transcript = self._transcript()
        caption = AsyncMock()
        caption.fetch = AsyncMock(return_value=transcript)
        assembly = AsyncMock()

        monkeypatch.setattr("app.tasks.transcription._live_pipeline_enabled", lambda: True)
        monkeypatch.setattr(
            "app.tasks.transcription.YouTubeCaptionTranscriptProvider", lambda: caption
        )
        monkeypatch.setattr("app.tasks.transcription.get_transcript_provider", lambda: assembly)

        result = await _fetch_transcript_with_retry("https://www.youtube.com/watch?v=x123")

        assert result is transcript
        caption.fetch.assert_awaited_once()
        assembly.fetch.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_to_assembly_when_no_captions(self, monkeypatch) -> None:
        from unittest.mock import AsyncMock

        caption = AsyncMock()
        caption.fetch = AsyncMock(
            side_effect=ExternalServiceError("no captions", service="youtube-captions")
        )
        assembly_transcript = self._transcript()
        assembly = AsyncMock()
        assembly.fetch = AsyncMock(return_value=assembly_transcript)

        monkeypatch.setattr("app.tasks.transcription._live_pipeline_enabled", lambda: True)
        monkeypatch.setattr(
            "app.tasks.transcription.YouTubeCaptionTranscriptProvider", lambda: caption
        )
        monkeypatch.setattr("app.tasks.transcription.get_transcript_provider", lambda: assembly)

        result = await _fetch_transcript_with_retry("https://www.youtube.com/watch?v=x123")

        assert result is assembly_transcript
        caption.fetch.assert_awaited_once()
        assembly.fetch.assert_awaited_once()
