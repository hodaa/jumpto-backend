"""Integration tests for API endpoints."""

from uuid import uuid4

import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from app.models import Video

settings = get_settings()


class TestCORS:
    """Tests for CORS configuration."""

    @pytest.mark.asyncio
    async def test_cors_preflight_allowed_origin(self, client: AsyncClient) -> None:
        response = await client.options(
            "/api/search",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    @pytest.mark.asyncio
    async def test_cors_preflight_disallowed_origin(self, client: AsyncClient) -> None:
        response = await client.options(
            "/api/search",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        # Should not have ACAO header for disallowed origin
        assert response.headers.get("access-control-allow-origin") != "http://evil.com"

    @pytest.mark.asyncio
    async def test_cors_simple_request_allowed_origin(self, client: AsyncClient) -> None:
        response = await client.get(
            "/health",
            headers={"Origin": "http://localhost:5173"},
        )
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    @pytest.mark.asyncio
    async def test_cors_simple_request_disallowed_origin(self, client: AsyncClient) -> None:
        response = await client.get(
            "/health",
            headers={"Origin": "http://evil.com"},
        )
        assert response.headers.get("access-control-allow-origin") != "http://evil.com"


class TestSearchEndpoint:
    """Tests for POST /api/search."""

    @pytest.mark.asyncio
    async def test_invalid_url_returns_422(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "not-a-url", "keyword": "test"},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_keyword_returns_422(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "keyword": ""},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_whitespace_only_keyword_returns_422(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "keyword": "   "},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_non_youtube_url_returns_400(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "https://example.com/video", "keyword": "test"},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "INVALID_YOUTUBE_URL"

    @pytest.mark.asyncio
    async def test_channel_url_returns_400(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "https://www.youtube.com/channel/UC123", "keyword": "test"},
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_watch_without_v_returns_400(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/search",
            json={"youtube_url": "https://www.youtube.com/watch", "keyword": "test"},
        )
        assert response.status_code == 400


class TestVideoSearchEndpoint:
    """Tests for GET /api/video/{video_id}/search."""

    @pytest.mark.asyncio
    async def test_missing_keyword_returns_400(self, client: AsyncClient) -> None:
        video_id = uuid4()
        response = await client.get(f"/api/video/{video_id}/search")
        assert response.status_code == 422  # Missing query param

    @pytest.mark.asyncio
    async def test_empty_keyword_returns_400(self, client: AsyncClient) -> None:
        video_id = uuid4()
        response = await client.get(f"/api/video/{video_id}/search?keyword=")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_whitespace_keyword_returns_400(self, client: AsyncClient) -> None:
        video_id = uuid4()
        response = await client.get(f"/api/video/{video_id}/search?keyword=   ")
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_nonexistent_video_returns_404(self, client: AsyncClient, db_session) -> None:
        video_id = uuid4()
        response = await client.get(f"/api/video/{video_id}/search?keyword=test")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_video_not_transcribed_returns_404(
        self,
        client: AsyncClient,
        db_session,
        seeded_video: Video,
    ) -> None:
        # seeded_video is created without transcript
        response = await client.get(f"/api/video/{seeded_video.id}/search?keyword=test")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "VIDEO_NOT_TRANSCRIBED"


class TestJobStatusEndpoint:
    """Tests for GET /api/status/{job_id}."""

    @pytest.mark.asyncio
    async def test_nonexistent_job_returns_404(self, client: AsyncClient) -> None:
        job_id = uuid4()
        response = await client.get(f"/api/status/{job_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "NOT_FOUND"


class TestHealthEndpoint:
    """Tests for health check."""

    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestNoResults:
    """Tests for the not_found status when no matches are found."""

    async def _seed_transcribed_video(self, db_session, suffix: str) -> Video:
        """Seed a transcribed English video with a single known word."""
        from datetime import UTC, datetime

        from app.models import TranscriptWord

        video_id = f"notfndvid{suffix}"
        video = Video(
            youtube_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            title="Not Found Test",
            language="en",
            transcript="hello world",
            transcribed_at=datetime.now(UTC),
        )
        db_session.add(video)
        await db_session.flush()
        for word_index, word in enumerate(["hello", "world"]):
            word_row = TranscriptWord(
                video_id=video.id,
                word_index=word_index,
                word=word,
                start_time=float(word_index),
                end_time=float(word_index + 0.5),
            )
            db_session.add(word_row)
        await db_session.flush()
        return video

    @pytest.mark.asyncio
    async def test_post_search_non_existent_keyword_returns_not_found(
        self,
        client: AsyncClient,
        db_session,
    ) -> None:
        """POST /api/search on a transcribed video with a missing keyword returns not_found."""
        video = await self._seed_transcribed_video(db_session, "01")

        response = await client.post(
            "/api/search",
            json={"youtube_url": video.youtube_url, "keyword": "nonexistent"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "not_found"
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_post_search_existing_keyword_still_returns_found(
        self,
        client: AsyncClient,
        db_session,
    ) -> None:
        """POST /api/search with an existing keyword still returns found."""
        video = await self._seed_transcribed_video(db_session, "02")

        response = await client.post(
            "/api/search",
            json={"youtube_url": video.youtube_url, "keyword": "hello"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "found"


class TestNoResults:
    """Tests for the not_found status when no matches are found."""

    async def _seed_transcribed_video(self, db_session, suffix: str) -> Video:
        """Seed a transcribed English video with a single known word."""
        from datetime import UTC, datetime

        from app.models import TranscriptWord

        video_id = f"notfndvid{suffix}"
        video = Video(
            youtube_url=f"https://www.youtube.com/watch?v={video_id}",
            video_id=video_id,
            title="Not Found Test",
            language="en",
            transcript="hello world",
            transcribed_at=datetime.now(UTC),
        )
        db_session.add(video)
        await db_session.flush()
        for word_index, word in enumerate(["hello", "world"]):
            word_row = TranscriptWord(
                video_id=video.id,
                word_index=word_index,
                word=word,
                start_time=float(word_index),
                end_time=float(word_index + 0.5),
            )
            db_session.add(word_row)
        await db_session.flush()
        return video

    @pytest.mark.asyncio
    async def test_post_search_non_existent_keyword_returns_not_found(
        self,
        client: AsyncClient,
        db_session,
    ) -> None:
        """POST /api/search on a transcribed video with a missing keyword returns not_found."""
        video = await self._seed_transcribed_video(db_session, "01")

        response = await client.post(
            "/api/search",
            json={"youtube_url": video.youtube_url, "keyword": "nonexistent"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "not_found"
        assert data["results"] == []

    @pytest.mark.asyncio
    async def test_post_search_existing_keyword_still_returns_found(
        self,
        client: AsyncClient,
        db_session,
    ) -> None:
        """POST /api/search with an existing keyword still returns found."""
        video = await self._seed_transcribed_video(db_session, "02")

        response = await client.post(
            "/api/search",
            json={"youtube_url": video.youtube_url, "keyword": "hello"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "found"
        assert len(data["results"]) == 1
