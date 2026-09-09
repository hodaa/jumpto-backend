"""Tests for the gzip request-body middleware."""

import gzip
import json

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.gzip_request import GzipRequestBodyMiddleware
from starlette.responses import JSONResponse


@pytest.mark.asyncio
async def test_gzip_body_is_decompressed_before_routing() -> None:
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return JSONResponse({"parsed": seen["body"]["title"]})

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route

    app = Starlette(
        middleware=[Middleware(GzipRequestBodyMiddleware)],
        routes=[Route("/", handler, methods=["POST"])],
    )

    payload = {"title": "hello", "words": ["a", "b", "c"]}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/",
            content=gzip.compress(json.dumps(payload).encode("utf-8")),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert seen["body"] == payload


@pytest.mark.asyncio
async def test_plain_body_is_untouched() -> None:
    seen = {}

    async def handler(request):
        seen["body"] = await request.json()
        return JSONResponse({"parsed": seen["body"]["title"]})

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route

    app = Starlette(
        middleware=[Middleware(GzipRequestBodyMiddleware)],
        routes=[Route("/", handler, methods=["POST"])],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/", json={"title": "howdy"}, headers={"Content-Encoding": "identity"}
        )

    assert response.status_code == 200
    assert seen["body"] == {"title": "howdy"}


@pytest.mark.asyncio
async def test_corrupt_gzip_body_falls_back_to_raw() -> None:
    seen = {}

    async def handler(request):
        seen["raw"] = await request.body()
        return JSONResponse({"ok": True})

    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.routing import Route

    app = Starlette(
        middleware=[Middleware(GzipRequestBodyMiddleware)],
        routes=[Route("/", handler, methods=["POST"])],
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/",
            content=b"not-gzip",
            headers={"Content-Encoding": "gzip"},
        )

    assert response.status_code == 200
    assert seen["raw"] == b"not-gzip"