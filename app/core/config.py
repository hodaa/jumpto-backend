"""Application configuration settings."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
        description="Redis connection URL",
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

    # External calls
    jumpto_live_external_calls: bool = Field(
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
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.environment.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
