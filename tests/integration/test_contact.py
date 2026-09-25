"""Integration tests for the public contact endpoint."""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ContactMessage


class TestCreateContactMessage:
    """Tests for POST /api/contact."""

    @pytest.mark.asyncio
    async def test_create_contact_message_persists(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "Sara",
                "email": "Sara@Example.com",
                "message": "Hello JumpTo!",
            },
        )
        assert response.status_code == 201
        payload = response.json()
        assert payload["id"]
        assert payload["created_at"]

        record = await db_session.execute(
            select(ContactMessage).where(ContactMessage.id == payload["id"])
        )
        stored = record.scalar_one()
        assert stored.email == "sara@example.com"
        assert stored.name == "Sara"
        assert stored.message == "Hello JumpTo!"

    @pytest.mark.asyncio
    async def test_rejects_malformed_email(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "Sara",
                "email": "not-an-email",
                "message": "Hello",
            },
        )
        assert response.status_code == 422
        assert b"Email" in response.content

    @pytest.mark.asyncio
    async def test_rejects_blank_name(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "   ",
                "email": "sara@example.com",
                "message": "Hello",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_message_too_long(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "Sara",
                "email": "sara@example.com",
                "message": "x" * 501,
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_rejects_whitespace_only_message(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "Sara",
                "email": "sara@example.com",
                "message": "   ",
            },
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_strips_leading_trailing_whitespace(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        response = await client.post(
            "/api/contact",
            json={
                "name": "  Sara  ",
                "email": " sara@example.com ",
                "message": "  Hello  ",
            },
        )
        assert response.status_code == 201
        record = await db_session.execute(select(func.count()).select_from(ContactMessage))
        assert record.scalar_one() >= 1


class TestContactCORS:
    """Tests for CORS on the contact endpoint."""

    @pytest.mark.asyncio
    async def test_cors_preflight_allows_configured_origin(self, client: AsyncClient) -> None:
        response = await client.options(
            "/api/contact",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    @pytest.mark.asyncio
    async def test_cors_preflight_rejects_unknown_origin(self, client: AsyncClient) -> None:
        response = await client.options(
            "/api/contact",
            headers={
                "Origin": "http://evil.com",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.headers.get("access-control-allow-origin") != "http://evil.com"
