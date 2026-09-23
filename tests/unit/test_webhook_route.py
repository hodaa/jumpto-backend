"""Unit tests for the Assembly webhook API route function."""

from uuid import UUID

import pytest

from app.api.webhooks import assembly_webhook
from app.models import Job
from app.schemas import AssemblyWebhookRequest, AssemblyWebhookStatus

_JOB_ID = UUID("12345678-1234-5678-1234-567812345678")
_VIDEO_ID = UUID("87654321-4321-8765-4321-876543218765")


class FakeJobService:
    """JobService double tracking whether a job was failed by the handler."""

    def __init__(self) -> None:
        self.job = Job(id=_JOB_ID, video_id=_VIDEO_ID, status="processing")
        self.fail_calls: list[tuple[UUID, str]] = []

    async def get_job(self, job_id: UUID) -> Job:
        return self.job

    async def fail_job(self, job_id: UUID, error: str) -> Job:
        self.fail_calls.append((job_id, error))
        return self.job


class FakeSession:
    """Async session double recording commit calls."""

    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class TestAssemblyWebhookRoute:
    """Direct invocation of the webhook route and its response/commit."""

    @pytest.mark.asyncio
    async def test_commits_and_acks_completed(self, monkeypatch) -> None:
        job_service = FakeJobService()
        session = FakeSession()
        dispatched: list = []
        monkeypatch.setattr(
            "app.services.webhooks.dispatch_resume_transcription",
            lambda job_id, transcript_id, provider: dispatched.append(
                (job_id, transcript_id, provider)
            ),
        )

        response = await assembly_webhook(
            AssemblyWebhookRequest(transcript_id="asm-123", status=AssemblyWebhookStatus.COMPLETED),
            job_id=_JOB_ID,
            provider="yt-dlp",
            job_service=job_service,
            session=session,
        )

        assert response.status == "ok"
        assert session.committed is True
        assert dispatched == [(_JOB_ID, "asm-123", "yt-dlp")]
        assert job_service.fail_calls == []
