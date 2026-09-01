"""YouTube URL parsing and validation utilities."""

import re
from dataclasses import dataclass

from app.core.exceptions import InvalidYouTubeURLError
from app.core.logging import get_logger

logger = get_logger(__name__)

# YouTube URL patterns
YOUTUBE_PATTERNS = [
    # Standard watch URLs: youtube.com/watch?v=VIDEO_ID
    re.compile(
        r"^(?:https?://)?(?:www\.)?youtube\.com/watch\?.*v=([a-zA-Z0-9_-]{11})",
        re.IGNORECASE,
    ),
    # Short URLs: youtu.be/VIDEO_ID
    re.compile(
        r"^(?:https?://)?youtu\.be/([a-zA-Z0-9_-]{11})",
        re.IGNORECASE,
    ),
    # Shorts URLs: youtube.com/shorts/VIDEO_ID
    re.compile(
        r"^(?:https?://)?(?:www\.)?youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
        re.IGNORECASE,
    ),
    # Embed URLs: youtube.com/embed/VIDEO_ID
    re.compile(
        r"^(?:https?://)?(?:www\.)?youtube\.com/embed/([a-zA-Z0-9_-]{11})",
        re.IGNORECASE,
    ),
]

# Patterns to explicitly reject
REJECTED_PATTERNS = [
    # Channel URLs
    re.compile(r"youtube\.com/(?:channel|user|c)/", re.IGNORECASE),
    # Watch URL without v parameter
    re.compile(r"youtube\.com/watch(?:\?|$)(?!.*v=)", re.IGNORECASE),
]


@dataclass
class YouTubeVideoInfo:
    """Parsed YouTube video information."""

    video_id: str
    original_url: str


def normalize_youtube_url(video_id: str) -> str:
    """
    Build a canonical watch URL for a video id, dropping playlist/tracking params.

    Playlist query parameters (``list``, ``index``, ``start_radio``, ``si``) can
    make yt-dlp hang or resolve the playlist instead of the single video, so the
    pipeline always operates on a clean ``watch?v=<id>`` URL.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_video_id(url: str) -> str | None:
    """
    Extract YouTube video ID from various URL formats.

    Args:
        url: YouTube URL string

    Returns:
        Video ID if found, None otherwise
    """
    for pattern in YOUTUBE_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def is_youtube_url(url: str) -> bool:
    """
    Check if URL is a valid YouTube video URL.

    Args:
        url: URL string to check

    Returns:
        True if valid YouTube video URL, False otherwise
    """
    # Check for rejected patterns first
    for pattern in REJECTED_PATTERNS:
        if pattern.search(url):
            return False

    # Check for accepted patterns
    return extract_video_id(url) is not None


def validate_youtube_url(url: str) -> YouTubeVideoInfo:
    """
    Validate and parse YouTube URL.

    Args:
        url: YouTube URL string

    Returns:
        YouTubeVideoInfo with video_id and original_url

    Raises:
        InvalidYouTubeURLError: If URL is not a valid YouTube video URL
    """
    # Check for rejected patterns first
    for pattern in REJECTED_PATTERNS:
        if pattern.search(url):
            logger.warning("Rejected YouTube URL pattern", url=url)
            raise InvalidYouTubeURLError(url)

    video_id = extract_video_id(url)
    if not video_id:
        logger.warning("Could not extract video ID from URL", url=url)
        raise InvalidYouTubeURLError(url)

    logger.debug("Parsed YouTube URL", video_id=video_id, url=url)
    return YouTubeVideoInfo(video_id=video_id, original_url=normalize_youtube_url(video_id))
