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
