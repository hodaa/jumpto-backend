"""Webhook API routes for provider completion callbacks."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.repositories import JobRepository, VideoRepository
from app.schemas import AssemblyWebhookRequest, WebhookAckResponse
from app.services import JobService
from app.services.webhooks import handle_assembly_webhook

router = APIRouter()


async def _get_job_service(session: AsyncSession = Depends(get_db_session)) -> JobService:
    """Build a job service bound to the request session."""
    return JobService(JobRepository(session), VideoRepository(session))


@router.post(
    "/api/webhooks/assembly",
    response_model=WebhookAckResponse,
    responses={
        404: {"description": "Job not found"},
        422: {"description": "Validation error"},
    },
)
async def assembly_webhook(
    payload: AssemblyWebhookRequest,
    job_id: Annotated[UUID, Query(description="Job UUID armed by the worker")],
    provider: Annotated[str, Query(description="Provider strategy name")] = "yt-dlp",
    job_service: JobService = Depends(_get_job_service),
    session: AsyncSession = Depends(get_db_session),
) -> WebhookAckResponse:
    """Receive Assembly.ai completion callbacks and resume or fail the job."""
    await handle_assembly_webhook(
        job_service,
        job_id=job_id,
        transcript_id=payload.transcript_id,
        status=payload.status,
        provider=provider,
    )
    await session.commit()
    return WebhookAckResponse(status="ok")
