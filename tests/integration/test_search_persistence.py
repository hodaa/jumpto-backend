"""Integration tests verifying the real DB session dependency commits writes."""

from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

import app.core.database as database_module
from app.main import create_app
from app.repositories import JobRepository
from tests.conftest import TestAsyncSessionFactory


@pytest.mark.asyncio
async def test_search_persists_job_via_real_session(test_db_engine, monkeypatch) -> None:
    """A 202 search response must persist its job to the database.

    Regression test for get_db_session not committing: the job was only ever
    flushed inside the request transaction, so a second session (or the worker)
    could never find it.
    """
    monkeypatch.setattr(database_module, "async_session_factory", TestAsyncSessionFactory)
    app = create_app()

    async with AsyncClient(base_url="http://test", transport=ASGITransport(app=app)) as client:
        response = await client.post(
            "/api/search",
            json={
                "youtube_url": "https://www.youtube.com/watch?v=persist1234",
                "keyword": "quick brown fox",
            },
        )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "processing"

    async with TestAsyncSessionFactory() as session:
        job = await JobRepository(session).get_by_id(UUID(payload["job_id"]))
        assert job is not None
        assert job.status == "pending"
