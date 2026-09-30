"""Application configuration settings."""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import lru_cache
from ipaddress import IPv4Network, IPv6Network, ip_network
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
    live_external_calls: Annotated[bool, BeforeValidator(_coerce_bool)] = Field(
        default=False,
        description="Enable live external API calls (yt-dlp, Assembly.ai)",
    )
    # Transcript mode
    transcript_mode: str = Field(
        default="real",
        description="Transcript mode: real or fake",
    )

    # Sessions
    session_cookie_name: str = Field(
        default="__Host-jumpto_session",
        description="Session cookie name. The __Host- prefix makes browsers enforce Secure/Path=/.",
    )
    session_absolute_ttl_days: int = Field(
        default=30,
        ge=1,
        description="Hard cap on session lifetime regardless of activity",
    )
    session_idle_ttl_days: int = Field(
        default=7,
        ge=1,
        description="Sliding window: session expires this long after last use",
    )
    session_sweep_interval_seconds: int = Field(
        default=3600,
        ge=60,
        description="How often to delete expired and revoked session rows",
    )

    # CSRF: a per-deployment secret echoed in a header on cookie-authenticated
    # mutations. Not a credential, so it may sit in the frontend bundle.
    csrf_token: str = Field(
        default="",
        description="Shared secret the web client sends as X-CSRF-Token",
    )
    csrf_header_name: str = Field(
        default="X-CSRF-Token",
        description="Header carrying the CSRF token on state-changing requests",
    )

    # Login lockout
    max_login_attempts: int = Field(
        default=3,
        ge=1,
        description="Consecutive wrong passwords before the account is locked",
    )
    login_ip_hourly_limit: int = Field(
        default=200,
        ge=1,
        description=(
            "Per-IP failed logins allowed per hour. Kept high: mobile carriers NAT "
            "many subscribers behind one address."
        ),
    )
    trusted_proxy_networks: str = Field(
        default="",
        description=(
            "Comma-separated IP addresses or CIDR ranges of proxies that may set "
            "X-Forwarded-For. Empty means trust none, so the header is ignored and "
            "the connecting address is used."
        ),
    )

    # Email (verification / password reset) via Gmail SMTP
    smtp_host: str = Field(default="smtp.gmail.com", description="SMTP host")
    smtp_port: int = Field(default=587, ge=1, description="SMTP port")
    smtp_user: str = Field(default="", description="SMTP account address")
    smtp_app_password: str = Field(
        default="",
        description="Gmail app password. This grants IMAP as well as SMTP - treat as a secret.",
    )
    email_from: str = Field(default="", description="Sender address for outgoing mail")
    email_from_name: str = Field(default="Qfza", description="Sender display name")
    public_site_url: str = Field(
        default="https://qfza.app",
        description="Public origin, used to build links inside emails",
    )
    password_reset_token_ttl_minutes: int = Field(
        default=30,
        ge=1,
        description="Expiry for password reset and email verification tokens",
    )

    # Google sign-in (identity only)
    google_client_id: str = Field(
        default="", description="Google OAuth client id for ID token validation"
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

    @property
    def session_absolute_ttl(self) -> timedelta:
        """Hard cap on session lifetime, however active the session is."""
        return timedelta(days=self.session_absolute_ttl_days)

    @property
    def session_idle_ttl(self) -> timedelta:
        """Sliding window after which an unused session stops resolving."""
        return timedelta(days=self.session_idle_ttl_days)

    @property
    def email_token_ttl(self) -> timedelta:
        """Lifetime of verification and reset links."""
        return timedelta(minutes=self.password_reset_token_ttl_minutes)

    @property
    def cookie_is_secure(self) -> bool:
        """Whether the session cookie should carry the Secure attribute."""
        return not self.is_development

    @property
    def trusted_proxy_list(self) -> list[IPv4Network | IPv6Network]:
        """Parse the trusted proxy list into networks.

        An unparseable entry is dropped rather than fatal: a typo in an env var
        should not stop the API booting, and dropping an entry only makes the
        deployment stricter.
        """
        networks: list[IPv4Network | IPv6Network] = []
        for raw in self.trusted_proxy_networks.split(","):
            entry = raw.strip()
            if not entry:
                continue
            try:
                networks.append(ip_network(entry, strict=False))
            except ValueError:
                # stdlib logging, not the app's: that wrapper imports settings,
                # and settings cannot depend on something that imports it.
                logging.getLogger(__name__).warning(
                    "Ignoring an unparseable trusted proxy entry", extra={"entry": entry}
                )
        return networks


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
