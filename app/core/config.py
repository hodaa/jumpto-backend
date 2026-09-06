"""Application configuration settings."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _coerce_bool(value: str | bool | int) -> bool:
    """Coerce empty-string / falsy env values to a valid bool."""
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("", "0", "false", "no"):
            return False
        if value in ("1", "true", "yes"):
            return True
    return bool(value)


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str = Field(
        default="postgresql://postgres:postgres@localhost:5432/jumpto",
        description="PostgreSQL connection URL",
    )

    # Redis
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL used as the Celery broker (when QUEUE_PROVIDER=redis)",
    )

    # Celery / broker
    queue_provider: Literal["redis", "rabbitmq"] = Field(
        default="redis",
        description="Celery broker transport: redis or rabbitmq (QUEUE_PROVIDER env var)",
    )
    rabbitmq_url: str = Field(
        default="amqp://guest:guest@localhost:5672//",
        description="RabbitMQ connection URL used as the Celery broker (when QUEUE_PROVIDER=rabbitmq)",
    )

    # Job / transcription timeout
    job_timeout_seconds: int = Field(
        default=600,
        ge=1,
        description="Max seconds a transcription job may run before it is failed",
    )

    # Assembly AI
    assembly_api_key: str = Field(
        default="",
        description="Assembly.ai API key for transcription",
    )

    # CORS
    cors_origins: str = Field(
        default="http://localhost:5173",
        description="Comma-separated list of allowed CORS origins",
    )

    # Environment
    environment: str = Field(
        default="development",
        description="Application environment (development/production)",
    )

    # Internal worker API key
    internal_api_key: str = Field(
        default="",
        description="Shared API key for internal worker-to-backend communication",
    )

    # External calls
    jumpto_live_external_calls: Annotated[bool, BeforeValidator(_coerce_bool)] = Field(
        default=False,
        description="Enable live external API calls (yt-dlp, Assembly.ai)",
    )
    # Transcript mode
    jumpto_transcript_mode: str = Field(
        default="real",
        description="Transcript mode: real or fake",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def broker_url(self) -> str:
        """Return the Celery broker URL for the configured queue provider."""
        if self.queue_provider == "rabbitmq":
            return self.rabbitmq_url
        return self.redis_url

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
