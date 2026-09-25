"""Unit tests for the job timeout setting."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestJobTimeoutSetting:
    """Tests for the per-job transcription timeout setting."""

    def test_default_is_ten_minutes(self) -> None:
        assert Settings(_env_file=None).job_timeout_seconds == 600

    def test_reads_configured_value_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "120")
        assert Settings(_env_file=None).job_timeout_seconds == 120

    def test_rejects_non_positive_values(self) -> None:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, job_timeout_seconds=0)


class TestBrokerUrl:
    """Tests for Celery broker URL selection."""

    def test_default_is_redis(self) -> None:
        assert Settings(_env_file=None).queue_provider == "redis"
        assert Settings(_env_file=None).broker_url == "redis://localhost:6379/0"

    def test_broker_url_uses_redis_url_for_redis_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "redis")
        monkeypatch.setenv("REDIS_URL", "rediss://user:pass@redis.example:6379/0")
        settings = Settings(_env_file=None)
        assert settings.broker_url == "rediss://user:pass@redis.example:6379/0"

    def test_rabbitmq_broker_url_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "rabbitmq")
        monkeypatch.setenv("RABBITMQ_URL", "amqp://user:pass@rabbit:5672//")
        settings = Settings(_env_file=None)
        assert settings.queue_provider == "rabbitmq"
        assert settings.broker_url == "amqp://user:pass@rabbit:5672//"

    def test_default_rabbitmq_broker_url(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "rabbitmq")
        assert Settings(_env_file=None).broker_url == "amqp://guest:guest@localhost:5672//"

    def test_rejects_invalid_queue_provider(self, monkeypatch) -> None:
        monkeypatch.setenv("QUEUE_PROVIDER", "kafka")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)


class TestLiveExternalCallsSetting:
    """Tests for the live external calls gate setting."""

    def test_defaults_to_false(self, monkeypatch) -> None:
        monkeypatch.delenv("LIVE_EXTERNAL_CALLS", raising=False)
        assert Settings(_env_file=None).live_external_calls is False

    def test_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("LIVE_EXTERNAL_CALLS", "true")
        assert Settings(_env_file=None).live_external_calls is True


class TestTranscriptModeSetting:
    """Tests for the transcript mode setting."""

    def test_defaults_to_real(self, monkeypatch) -> None:
        monkeypatch.delenv("TRANSCRIPT_MODE", raising=False)
        assert Settings(_env_file=None).transcript_mode == "real"

    def test_reads_env_var(self, monkeypatch) -> None:
        monkeypatch.setenv("TRANSCRIPT_MODE", "fake")
        assert Settings(_env_file=None).transcript_mode == "fake"
