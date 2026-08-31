"""Pydantic schemas for request/response validation."""

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class JobStatus(str, Enum):
    """Job status enumeration."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class SearchStatus(str, Enum):
    """Search result status enumeration."""

    FOUND = "found"
    PROCESSING = "processing"
    NOT_FOUND = "not_found"
    LANGUAGE_MISMATCH = "language_mismatch"


class SearchLanguage(str, Enum):
    """User-selectable search/transcript languages."""

    EN = "en"
    AR = "ar"


class TimestampResult(BaseModel):
    """Single timestamp result for a keyword match."""

    model_config = ConfigDict(from_attributes=True)

    timestamp: str = Field(..., description="Formatted timestamp (MM:SS)")
    progress_seconds: float = Field(..., description="Progress in seconds")
    text_snippet: str | None = Field(None, description="Surrounding text context")


class SearchRequest(BaseModel):
    """Request schema for POST /api/search."""

    model_config = ConfigDict(str_strip_whitespace=True)

    youtube_url: HttpUrl = Field(..., description="YouTube video URL")
    keyword: str = Field(..., min_length=1, description="Keyword or phrase to search")
    language: SearchLanguage = Field(
        default=SearchLanguage.EN,
        description="Language the user is searching in",
    )

    @field_validator("keyword")
    @classmethod
    def validate_keyword_not_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Keyword cannot be empty or whitespace only")
        return v.strip()


class SearchResponseCached(BaseModel):
    """Response schema for cached search results."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.FOUND
    results: list[TimestampResult] = Field(default_factory=list)


class SearchResponseLanguageMismatch(BaseModel):
    """Response schema when the selected language does not match the video."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.LANGUAGE_MISMATCH
    video_language: str | None = None
    message: str = "The video is not in the selected language"


class SearchResponseProcessing(BaseModel):
    """Response schema for search that triggered a job."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.PROCESSING
    job_id: UUID
    video_id: UUID


class VideoSearchResponse(BaseModel):
    """Response schema for GET /api/video/{video_id}/search."""

    model_config = ConfigDict(from_attributes=True)

    status: SearchStatus = SearchStatus.FOUND
    results: list[TimestampResult] = Field(default_factory=list)


class StatusResponse(BaseModel):
    """Response schema for GET /api/status/{job_id}."""

    model_config = ConfigDict(from_attributes=True)

    status: JobStatus
    video_id: UUID
    progress: int | None = None
    results: list[TimestampResult] | None = None
    error: str | None = None


class VideoResponse(BaseModel):
    """Video information response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    youtube_url: str
    video_id: str
    title: str | None = None
    duration_seconds: int | None = None
    transcribed_at: datetime | None = None
    created_at: datetime


class JobResponse(BaseModel):
    """Job information response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    video_id: UUID
    status: JobStatus
    progress: int | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime


class ErrorResponse(BaseModel):
    """Error response schema."""

    model_config = ConfigDict(from_attributes=True)

    error: dict[str, Any]


# Union types for OpenAPI documentation
SearchResponse = SearchResponseCached | SearchResponseProcessing | SearchResponseLanguageMismatch
VideoSearchResponseUnion = VideoSearchResponse | SearchResponseLanguageMismatch
