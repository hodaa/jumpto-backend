"""API routes for the JumpTo application."""

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db_session
from app.core.exceptions import VideoNotFoundError, VideoNotTranscribedError
from app.models import Video
from app.repositories import JobRepository, TranscriptWordRepository, VideoRepository
from app.schemas import (
    FullTextSearchResponse,
    SearchRequest,
    SearchResponse,
    SearchResponseCached,
    SearchResponseProcessing,
    SearchStatus,
    StatusResponse,
    VideoSearchResponse,
)
from app.services import (
    FullTextSearchService,
    JobService,
    SearchService,
    validate_youtube_url,
)
from app.services.messaging import dispatch_transcription

router = APIRouter()


def get_video_repo(session: AsyncSession = Depends(get_db_session)) -> VideoRepository:
    """Build a video repository for the request session."""
    return VideoRepository(session)


def get_transcript_repo(
    session: AsyncSession = Depends(get_db_session),
) -> TranscriptWordRepository:
    """Build a transcript repository for the request session."""
    return TranscriptWordRepository(session)


def get_job_repo(session: AsyncSession = Depends(get_db_session)) -> JobRepository:
    """Build a job repository for the request session."""
    return JobRepository(session)


def get_search_service(
    transcript_repo: TranscriptWordRepository = Depends(get_transcript_repo),
) -> SearchService:
    """Build the search service."""
    return SearchService(transcript_repo)


def get_fulltext_service(
    video_repo: VideoRepository = Depends(get_video_repo),
) -> FullTextSearchService:
    """Build the full-text catalog search service."""
    return FullTextSearchService(video_repo)


def get_job_service(
    job_repo: JobRepository = Depends(get_job_repo),
    video_repo: VideoRepository = Depends(get_video_repo),
) -> JobService:
    """Build the job service."""
    return JobService(job_repo, video_repo)


@router.post(
    "/api/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    responses={
        200: {"model": SearchResponseCached, "description": "Cached results found"},
        202: {"model": SearchResponseProcessing, "description": "Job created, processing started"},
        400: {"description": "Invalid YouTube URL"},
        422: {"description": "Validation error"},
    },
)
async def search(
    request: SearchRequest,
    video_repo: VideoRepository = Depends(get_video_repo),
    search_service: SearchService = Depends(get_search_service),
    job_service: JobService = Depends(get_job_service),
    session: AsyncSession = Depends(get_db_session),
) -> SearchResponse:
    """
    Search for a keyword in a YouTube video transcript (cache-first).

    Returns cached results immediately when the video is already transcribed;
    otherwise queues a transcription job and returns a job id.
    """
    youtube_info = validate_youtube_url(str(request.youtube_url))
    video = await video_repo.get_by_video_id_lite(youtube_info.video_id)

    if video and video.transcribed_at:
        results = await search_service.search(video.id, request.keyword)
        if not results:
            return SearchResponseCached(status=SearchStatus.NOT_FOUND, results=[])
        return SearchResponseCached(status="found", results=results)

    if not video:
        video = await _get_or_create_video(
            video_repo,
            youtube_info.original_url,
            youtube_info.video_id,
        )

    job = await job_service.create_or_get_job(video.id)
    await session.commit()
    await asyncio.to_thread(_dispatch_pipeline, job.id)
    response = SearchResponseProcessing(status="processing", job_id=job.id, video_id=video.id)
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content=response.model_dump(mode="json"),
    )


@router.get(
    "/api/status/{job_id}",
    response_model=StatusResponse,
    responses={
        200: {"model": StatusResponse, "description": "Job status retrieved"},
        404: {"description": "Job not found"},
    },
)
async def get_job_status(
    job_id: UUID,
    job_service: JobService = Depends(get_job_service),
) -> StatusResponse:
    """Return the current status of a transcription job."""
    job = await job_service.get_job(job_id)
    return StatusResponse(
        status=job.status,
        video_id=job.video_id,
        progress=job.progress,
        results=None,
        error=job.error,
    )


@router.get(
    "/api/video/{video_id}/search",
    response_model=VideoSearchResponse,
    responses={
        200: {"model": VideoSearchResponse, "description": "Search results"},
        400: {"description": "Missing or invalid keyword"},
        404: {"description": "Video not found or not transcribed"},
    },
)
async def search_video(
    video_id: UUID,
    keyword: str = Query(..., description="Keyword or phrase to search"),
    video_repo: VideoRepository = Depends(get_video_repo),
    search_service: SearchService = Depends(get_search_service),
) -> VideoSearchResponse:
    """Search for a keyword within a specific transcribed video."""
    if not keyword.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Keyword cannot be empty or whitespace only",
        )

    video = await video_repo.get_by_id_lite(video_id)
    if not video:
        raise VideoNotFoundError(str(video_id))

    if not video.transcribed_at:
        raise VideoNotTranscribedError(str(video_id))

    results = await search_service.search(video_id, keyword.strip())
    if not results:
        return VideoSearchResponse(status=SearchStatus.NOT_FOUND, results=[])
    return VideoSearchResponse(status="found", results=results)


@router.get(
    "/api/videos/search",
    response_model=FullTextSearchResponse,
    responses={422: {"description": "Validation error"}},
)
async def search_videos_by_text(
    q: str = Query(..., min_length=1, max_length=200, description="Full-text query"),
    limit: int = Query(10, ge=1, le=50, description="Max results"),
    fulltext_service: FullTextSearchService = Depends(get_fulltext_service),
) -> FullTextSearchResponse:
    """Search transcribed videos by transcript content via PostgreSQL FTS.

    Query semantics follow ``websearch_to_tsquery``: plain words AND together,
    an explicit ``OR`` unions terms, double-quoted phrases match exactly,
    and stemming is applied.
    """
    results = await fulltext_service.search(q, limit=limit)
    status = SearchStatus.FOUND if results else SearchStatus.NOT_FOUND
    return FullTextSearchResponse(status=status, query=q.strip(), results=results)


async def _get_or_create_video(
    video_repo: VideoRepository,
    youtube_url: str,
    youtube_id: str,
    language: str = "en",
) -> Video:
    """Create a video record, resolving a concurrent-creation race."""
    try:
        return await video_repo.create(
            youtube_url=youtube_url, video_id=youtube_id, language=language
        )
    except IntegrityError:
        await video_repo.session.rollback()
        video = await video_repo.get_by_video_id_lite(youtube_id)
        if video:
            return video
        raise


def _dispatch_pipeline(job_id: UUID) -> None:
    """Publish the transcription job to the standalone worker."""
    dispatch_transcription(job_id)
