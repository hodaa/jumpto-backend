"""Unit tests for the Assembly webhook handling service."""

from uuid import UUID

import pytest

from app.core.exceptions import JobNotFoundError
from app.models import Job
from app.schemas import AssemblyWebhookStatus
from app.services import webhooks

_JOB_ID = UUID("12345678-1234-5678-1234-567812345678")
_VIDEO_ID = UUID("87654321-4321-8765-4321-876543218765")
_RESUME_TOKEN = "asm-123"


class FakeJobService:
    """JobService double recording fail_job calls for unit tests."""

    def __init__(self, job: Job | None) -> None:
        self.job = job
        self.failed: list[tuple[UUID, str]] = []

    async def get_job(self, job_id: UUID) -> Job:
        if self.job is None:
            raise JobNotFoundError(str(job_id))
        return self.job

    async def fail_job(self, job_id: UUID, error: str) -> Job:
        self.failed.append((job_id, error))
        return self.job


def _make_job(status: str) -> Job:
    return Job(id=_JOB_ID, video_id=_VIDEO_ID, status=status)


def _capture_dispatch(monkeypatch, captures: list) -> None:
    monkeypatch.setattr(
        webhooks,
        "dispatch_resume_transcription",
        lambda job_id, transcript_id, provider: captures.append((job_id, transcript_id, provider)),
    )


class TestHandleAssemblyWebhook:
    """Behavior of Assembly completion webhook routing."""

    @pytest.mark.asyncio
    async def test_completed_reenqueues_with_resume_token(self, monkeypatch) -> None:
        service = FakeJobService(_make_job("processing"))
        dispatched = []
        _capture_dispatch(monkeypatch, dispatched)

        await webhooks.handle_assembly_webhook(
            service,
            job_id=_JOB_ID,
            transcript_id=_RESUME_TOKEN,
            status=AssemblyWebhookStatus.COMPLETED,
            provider="yt-dlp",
        )

        assert dispatched == [(_JOB_ID, _RESUME_TOKEN, "yt-dlp")]
        assert service.failed == []

    @pytest.mark.asyncio
    async def test_completed_on_pending_job_reenqueues(self, monkeypatch) -> None:
        service = FakeJobService(_make_job("pending"))
        dispatched = []
        _capture_dispatch(monkeypatch, dispatched)

        await webhooks.handle_assembly_webhook(
            service,
            job_id=_JOB_ID,
            transcript_id=_RESUME_TOKEN,
            status=AssemblyWebhookStatus.COMPLETED,
            provider="yt-dlp",
        )

        assert dispatched == [(_JOB_ID, _RESUME_TOKEN, "yt-dlp")]

    @pytest.mark.asyncio
    async def test_error_fails_job(self, monkeypatch) -> None:
        service = FakeJobService(_make_job("processing"))
        dispatched = []
        _capture_dispatch(monkeypatch, dispatched)

        await webhooks.handle_assembly_webhook(
            service,
            job_id=_JOB_ID,
            transcript_id=_RESUME_TOKEN,
            status=AssemblyWebhookStatus.ERROR,
            provider="yt-dlp",
        )

        assert dispatched == []
        assert service.failed == [(_JOB_ID, webhooks._WEBHOOK_ERROR_MESSAGE)]

    @pytest.mark.asyncio
    async def test_terminal_job_is_untouched(self, monkeypatch) -> None:
        service = FakeJobService(_make_job("completed"))
        dispatched = []
        _capture_dispatch(monkeypatch, dispatched)

        await webhooks.handle_assembly_webhook(
            service,
            job_id=_JOB_ID,
            transcript_id=_RESUME_TOKEN,
            status=AssemblyWebhookStatus.COMPLETED,
            provider="yt-dlp",
        )

        assert dispatched == []
        assert service.failed == []

    @pytest.mark.asyncio
    async def test_failed_job_is_untouched(self, monkeypatch) -> None:
        service = FakeJobService(_make_job("failed"))
        dispatched = []
        _capture_dispatch(monkeypatch, dispatched)

        await webhooks.handle_assembly_webhook(
            service,
            job_id=_JOB_ID,
            transcript_id=_RESUME_TOKEN,
            status=AssemblyWebhookStatus.ERROR,
            provider="yt-dlp",
        )

        assert dispatched == []
        assert service.failed == []

    @pytest.mark.asyncio
    async def test_missing_job_raises_not_found(self, monkeypatch) -> None:
        service = FakeJobService(None)
        _capture_dispatch(monkeypatch, [])

        with pytest.raises(JobNotFoundError):
            await webhooks.handle_assembly_webhook(
                service,
                job_id=_JOB_ID,
                transcript_id=_RESUME_TOKEN,
                status=AssemblyWebhookStatus.COMPLETED,
                provider="yt-dlp",
            )
