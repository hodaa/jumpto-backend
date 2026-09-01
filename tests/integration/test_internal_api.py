"""Integration tests for the internal worker API endpoints."""

from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Job, Video
from app.schemas import JobStatus

settings = get_settings()

_INTERNAL_KEY = settings.internal_api_key


def _headers() -> dict:
    """Build internal API auth headers for a tested target."""
    return {"X-Internal-API-Key": _INTERNAL_KEY}


async def _create_job_with_video(
    db_session: AsyncSession, *, status: JobStatus = JobStatus.PENDING
) -> tuple[Video, Job]:
    """Create a video with a unique URL plus a linked job, returning both."""
    uid = uuid4().hex[:12]
    video = Video(
        youtube_url=f"https://www.youtube.com/watch?v={uid}",
        video_id=uid,
        language="en",
    )
    db_session.add(video)
    await db_session.flush()

    job = Job(video_id=video.id, status=status.value)
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(video)
    await db_session.refresh(job)
    return video, job


class TestInternalAuth:
    """Tests for internal API key enforcement."""

    @pytest.mark.asyncio
    async def test_rejects_missing_key(self, client: AsyncClient) -> None:
        response = await client.get("/internal/jobs/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_key(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/jobs/00000000-0000-0000-0000-000000000000",
            headers={"X-Internal-API-Key": "wrong"},
        )

        assert response.status_code == 401


class TestInternalGetJob:
    """Tests for GET /internal/jobs/{job_id}."""

    @pytest.mark.asyncio
    async def test_returns_job_and_video_data(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, job = await _create_job_with_video(db_session)

        response = await client.get(f"/internal/jobs/{job.id}", headers=_headers())

        assert response.status_code == 200
        data = response.json()
        assert data["job_id"] == str(job.id)
        assert data["video_id"] == str(video.id)
        assert data["youtube_video_id"] == video.video_id
        assert data["youtube_url"] == video.youtube_url
        assert data["status"] == "pending"

    @pytest.mark.asyncio
    async def test_returns_404_for_missing_job(self, client: AsyncClient) -> None:
        response = await client.get(
            "/internal/jobs/00000000-0000-0000-0000-000000000000", headers=_headers()
        )

        assert response.status_code == 404


class TestInternalLifecycle:
    """Tests for job lifecycle mutation endpoints."""

    @pytest.mark.asyncio
    async def test_advance_complete_fail_flow(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session)

        advance = await client.post(f"/internal/jobs/{job.id}/advance", headers=_headers())
        assert advance.status_code == 200
        assert advance.json()["status"] == "processing"

        complete = await client.post(f"/internal/jobs/{job.id}/complete", headers=_headers())
        assert complete.status_code == 200
        assert complete.json()["status"] == "completed"

        fail = await client.post(
            f"/internal/jobs/{job.id}/fail",
            json={"error": "Something went wrong"},
            headers=_headers(),
        )
        assert fail.status_code == 200
        assert fail.json()["status"] == "failed"

    @pytest.mark.asyncio
    async def test_store_transcript_updates_video(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        video, job = await _create_job_with_video(db_session, status=JobStatus.PROCESSING)

        response = await client.post(
            f"/internal/jobs/{job.id}/transcript",
            json={
                "title": "Seeded Video Title",
                "duration_seconds": 120,
                "language": "en",
                "transcript_text": "hello world",
                "words": [
                    {"word_index": 0, "word": "hello", "start_time": 0.0, "end_time": 0.5},
                    {"word_index": 1, "word": "world", "start_time": 0.5, "end_time": 1.0},
                ],
            },
            headers=_headers(),
        )

        assert response.status_code == 200

        await db_session.refresh(video)
        assert video.title == "Seeded Video Title"
        assert video.duration_seconds == 120
        assert video.language == "en"
        assert video.transcript == "hello world"
        assert video.transcribed_at is not None

    @pytest.mark.asyncio
    async def test_complete_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/complete", headers=_headers()
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_fail_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/fail",
            json={"error": "Something went wrong"},
            headers=_headers(),
        )
        assert response.status_code == 404


class TestInternalProgress:
    """Tests for the incremental progress reporting endpoint."""

    @pytest.mark.asyncio
    async def test_updates_progress_without_changing_status(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        _, job = await _create_job_with_video(db_session, status=JobStatus.PENDING)

        response = await client.post(
            f"/internal/jobs/{job.id}/progress", json={"progress": 40}, headers=_headers()
        )

        assert response.status_code == 200
        assert response.json()["status"] == "pending"
        await db_session.refresh(job)
        assert job.progress == 40

    @pytest.mark.asyncio
    async def test_missing_job_returns_404(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/progress",
            json={"progress": 40},
            headers=_headers(),
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_out_of_range_progress(self, client: AsyncClient) -> None:
        response = await client.post(
            "/internal/jobs/00000000-0000-0000-0000-000000000000/progress",
            json={"progress": 150},
            headers=_headers(),
        )
        assert response.status_code == 422
