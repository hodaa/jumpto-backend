"""Transcript providers (Assembly.ai live / deterministic fake)."""

import asyncio
import contextlib
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.core.config import get_settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

_ASSEMBLY_BASE_URL = "https://api.assemblyai.com/v2"
_POLL_INTERVAL_SECONDS = 5
_MAX_POLL_ATTEMPTS = 120
_UPLOAD_TIMEOUT_SECONDS = 300


@dataclass
class TranscriptWordData:
    """Single word with timing information."""

    word: str
    start_time: float
    end_time: float


@dataclass
class TranscriptData:
    """Full transcript with per-word timestamps."""

    language: str
    text: str
    words: list[TranscriptWordData]


class TranscriptProvider(ABC):
    """Abstract transcript source."""

    @abstractmethod
    async def fetch(self, youtube_url: str) -> TranscriptData:
        """Fetch transcript data for a YouTube URL."""


class FakeTranscriptProvider(TranscriptProvider):
    """Deterministic transcript used when external calls are disabled."""

    _SENTENCE_WORDS = [
        "the",
        "quick",
        "brown",
        "fox",
        "jumps",
        "over",
        "the",
        "lazy",
        "dog",
        "hello",
        "world",
        "welcome",
        "to",
        "jumpto",
        "find",
        "exact",
        "timestamps",
        "for",
        "any",
        "phrase",
        "in",
        "the",
        "video",
        "good",
        "luck",
        "with",
        "your",
        "searches",
    ]
    _WORD_GAP_SECONDS = 0.5

    async def fetch(self, youtube_url: str) -> TranscriptData:
        """Build a deterministic transcript from a fixed corpus."""
        words = [
            TranscriptWordData(
                word=word,
                start_time=index * self._WORD_GAP_SECONDS,
                end_time=(index + 1) * self._WORD_GAP_SECONDS,
            )
            for index, word in enumerate(self._SENTENCE_WORDS)
        ]
        text = " ".join(word.word for word in words)
        return TranscriptData(language="en", text=text, words=words)


class AssemblyTranscriptProvider(TranscriptProvider):
    """Real Assembly.ai transcription client (word-level timestamps)."""

    def __init__(self, api_key: str, base_url: str = _ASSEMBLY_BASE_URL) -> None:
        self.api_key = api_key
        self.base_url = base_url

    async def fetch(self, youtube_url: str) -> TranscriptData:
        """Download the audio and transcribe it via Assembly.ai."""
        audio_path = await asyncio.to_thread(_download_audio, youtube_url)
        try:
            headers = {"authorization": self.api_key}
            async with httpx.AsyncClient() as client:
                upload_url = await self._upload(client, headers, audio_path)
                transcript_id = await self._submit(client, headers, upload_url)
                return await self._poll(client, headers, transcript_id)
        finally:
            _remove_file(audio_path)

    async def _upload(self, client: httpx.AsyncClient, headers: dict, path: str) -> str:
        """Upload an audio file and return its public upload_url."""
        audio = Path(path).read_bytes()
        response = await client.post(
            f"{self.base_url}/upload",
            headers={**headers, "content-type": "application/octet-stream"},
            content=audio,
            timeout=_UPLOAD_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            logger.error("Assembly upload failed", status_code=response.status_code)
            raise ExternalServiceError("Audio upload failed", service="assemblyai")
        upload_url = str(response.json().get("upload_url") or "")
        if not upload_url:
            logger.error("Assembly upload returned no url")
            raise ExternalServiceError("Audio upload failed", service="assemblyai")
        return upload_url

    async def _submit(self, client: httpx.AsyncClient, headers: dict, audio_url: str) -> str:
        """Create a transcription job and return its id."""
        response = await client.post(
            f"{self.base_url}/transcript",
            headers=headers,
            json={"audio_url": audio_url},
        )
        if response.status_code != 200:
            logger.error(
                "Assembly submit failed", status_code=response.status_code, body=response.text[:300]
            )
            raise ExternalServiceError(
                "Transcription service rejected the request", service="assemblyai"
            )
        return response.json()["id"]

    async def _poll(
        self, client: httpx.AsyncClient, headers: dict, transcript_id: str
    ) -> TranscriptData:
        """Poll until the transcript is ready and parse word timestamps."""
        for _ in range(_MAX_POLL_ATTEMPTS):
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            response = await client.get(
                f"{self.base_url}/transcript/{transcript_id}", headers=headers
            )
            if response.status_code != 200:
                continue
            data = response.json()
            if data["status"] == "completed":
                return _parse_assembly_transcript(data)
            if data["status"] == "error":
                raise ExternalServiceError("Transcription service failed", service="assemblyai")
        raise ExternalServiceError("Transcription timed out", service="assemblyai")


def _download_audio(youtube_url: str) -> str:
    """Download a YouTube audio stream to a temp file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".webm")
    os.close(fd)
    destination = Path(path)
    destination.unlink(missing_ok=True)
    options: dict = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "outtmpl": path,
    }
    try:
        _run_download(options, youtube_url)
        if not destination.exists() or destination.stat().st_size == 0:
            logger.error("Audio download produced no file", path=path)
            raise ExternalServiceError("Audio download produced no file", service="yt-dlp")
        return path
    except ExternalServiceError:
        _remove_file(path)
        raise
    except Exception as exc:
        _remove_file(path)
        logger.error("Audio download failed", error=str(exc))
        raise ExternalServiceError("Could not download audio", service="yt-dlp") from exc


def _run_download(options: dict, youtube_url: str) -> None:
    """Run a yt-dlp audio download for a URL."""
    import yt_dlp

    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([youtube_url])


def _remove_file(path: str) -> None:
    """Best-effort removal of a temp audio file."""
    with contextlib.suppress(OSError):
        Path(path).unlink()


def _parse_assembly_transcript(data: dict) -> TranscriptData:
    """Convert an Assembly.ai response into TranscriptData."""
    words = [
        TranscriptWordData(
            word=raw["text"],
            start_time=float(raw["start"]) / 1000.0,
            end_time=float(raw["end"]) / 1000.0,
        )
        for raw in data.get("words", [])
    ]
    return TranscriptData(
        language=data.get("language_code", "en"),
        text=str(data.get("text") or ""),
        words=words,
    )


def get_transcript_provider() -> TranscriptProvider:
    """
    Return the transcript provider for the current configuration.

    Returns the fake provider unless fake mode is off AND live calls are
    explicitly enabled with an Assembly.ai API key.
    """
    settings = get_settings()
    if settings.jumpto_transcript_mode.lower() == "fake":
        return FakeTranscriptProvider()
    if settings.jumpto_live_external_calls and settings.assembly_api_key:
        return AssemblyTranscriptProvider(settings.assembly_api_key)
    logger.warning(
        "Live transcription not configured; falling back to fake provider",
        live_external_calls=settings.jumpto_live_external_calls,
        has_api_key=bool(settings.assembly_api_key),
    )
    return FakeTranscriptProvider()
