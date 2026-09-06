"""Unit tests for async database URL translation."""

import ssl

from app.core.database import build_async_database_url


class TestBuildAsyncDatabaseUrl:
    """Tests for build_async_database_url function."""

    def test_plain_url_uses_asyncpg_scheme(self) -> None:
        url = "postgresql://user:pass@localhost:5432/db"
        async_url, connect_args = build_async_database_url(url)
        assert async_url.startswith("postgresql+asyncpg://")
        assert connect_args == {}

    def test_sslmode_require_maps_to_ssl_true(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=require"
        async_url, connect_args = build_async_database_url(url)
        assert "sslmode" not in async_url
        assert connect_args == {"ssl": True}

    def test_sslmode_disable_maps_to_no_ssl(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=disable"
        async_url, connect_args = build_async_database_url(url)
        assert connect_args == {}

    def test_sslmode_verify_full_creates_ssl_context(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=verify-full"
        async_url, connect_args = build_async_database_url(url)
        assert not isinstance(connect_args["ssl"], bool)
        assert connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED

    def test_strips_non_sslmode_query_params(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=require&channel_binding=require"
        async_url, connect_args = build_async_database_url(url)
        assert "sslmode" not in async_url
        assert "channel_binding" not in async_url
        assert connect_args == {"ssl": True}

    def test_unknown_sslmode_defaults_to_verify_required(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=bogus"
        async_url, connect_args = build_async_database_url(url)
        assert not isinstance(connect_args["ssl"], bool)
        assert connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED
