"""Alembic environment configuration."""

import asyncio
import sys
from logging.config import fileConfig
from pathlib import Path

from pydantic_settings import BaseSettings
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Add backend to path
sys.path.append(str(Path(__file__).parent.parent))

from app.core.config import Settings
from app.core.database import Base, build_async_database_url
from app.models import (  # noqa: F401
    AuthToken,
    ContactMessage,
    Job,
    SearchHistory,
    Session,
    TranscriptWord,
    User,
    Video,
)


class _MigrationSettings(BaseSettings):
    """Only what migrations need: the database URL.

    Deliberately does not build the full ``Settings``, so running
    ``alembic upgrade head`` does not require runtime secrets such as
    ``CSRF_TOKEN`` that migrations never use. Reuses ``Settings``'
    ``model_config`` so ``DATABASE_URL`` resolves from the environment
    and ``.env`` exactly as it does in the application.
    """

    model_config = Settings.model_config

    database_url: str = "postgresql://postgres:postgres@localhost:5432/jumpto"


# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Resolve the database URL (env var or .env, same as the app) without
# constructing the full application settings.
migration_settings = _MigrationSettings()
async_url, _ = build_async_database_url(migration_settings.database_url)
config.set_main_option("sqlalchemy.url", async_url)

# Add your model's MetaData object here
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    _, connect_args = build_async_database_url(migration_settings.database_url)
    engine_options: dict[str, object] = {"poolclass": pool.NullPool}
    if connect_args:
        engine_options["connect_args"] = connect_args

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        **engine_options,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
