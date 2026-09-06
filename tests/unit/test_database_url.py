"""Unit tests for async database URL translation."""

import ssl
from pathlib import Path

from app.core.database import _run_alembic_upgrade, build_async_database_url


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


class TestRunAlembicUpgrade:
    """Tests for the programmatic Alembic upgrade helper."""

    def test_upgrades_to_head_at_project_root(self, monkeypatch) -> None:
        calls = []
        project_root = Path(__file__).resolve().parents[2]

        class FakeConfig:
            def __init__(self, ini_path: str) -> None:
                calls.append(("config", ini_path))

            def set_main_option(self, key: str, value: str) -> None:
                calls.append((key, value))

        monkeypatch.setattr("alembic.config.Config", FakeConfig)
        monkeypatch.setattr(
            "alembic.command.upgrade", lambda config, revision: calls.append(("upgrade", revision))
        )

        _run_alembic_upgrade()

        ini = str(project_root / "alembic.ini")
        script_location = str(project_root / "alembic")
        assert calls == [
            ("config", ini),
            ("script_location", script_location),
            ("upgrade", "head"),
        ]

    def test_unknown_sslmode_defaults_to_verify_required(self) -> None:
        url = "postgresql://user:pass@host/db?sslmode=bogus"
        async_url, connect_args = build_async_database_url(url)
        assert not isinstance(connect_args["ssl"], bool)
        assert connect_args["ssl"].verify_mode == ssl.CERT_REQUIRED
