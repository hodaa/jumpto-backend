"""ASGI middleware that decompresses gzip-encoded request bodies.

Starlette/FastAPI do not decompress request bodies automatically. The worker's
backend client gzips transcript POSTs larger than 1 MB so they stay under
Vercel's request-body wire-size cap; without decompression on this side those
requests would fail JSON parsing with a 400/422.

Implemented as a raw ASGI wrapper around ``receive`` (rather than a
``BaseHTTPMiddleware`` that mutates ``request._body``) so every ``Request``
built from the same scope sees the decompressed body, regardless of instance.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

ReceiveLike = Callable[[], Any]


class GzipRequestBodyMiddleware:
    """Decompress ``Content-Encoding: gzip`` request bodies before routing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._is_gzip_request(scope):
            await self.app(scope, receive, send)
            return
        raw = await self._drain(receive)
        try:
            body = gzip.decompress(raw) if raw else b""
        except OSError:
            body = raw
        await self.app(scope, self._serve(body), send)

    @staticmethod
    def _is_gzip_request(scope: Scope) -> bool:
        for key, value in scope.get("headers", []):
            if key == b"content-encoding" and value.lower() == b"gzip":
                return True
        return False

    @staticmethod
    async def _drain(receive: Receive) -> bytes:
        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                continue
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        return b"".join(chunks)

    @staticmethod
    def _serve(body: bytes) -> ReceiveLike:
        sent = False

        async def receive() -> dict[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return receive