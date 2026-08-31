"""Unit tests for external service providers (fake/live switch)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.exceptions import ExternalServiceError
from app.providers.media import get_media_info
from app.providers.transcript import (
    AssemblyTranscriptProvider,
    FakeTranscriptProvider,
    _parse_assembly_transcript,
    get_transcript_provider,
)

_FAKE_SETTINGS_FAKE_MODE = SimpleNamespace(
    jumpto_transcript_mode="fake",
    jumpto_live_external_calls=True,
    assembly_api_key="secret-key",
)


def _settings(*, mode: str, live: bool, api_key: str) -> SimpleNamespace:
    """Build a minimal settings object for provider selection."""
    return SimpleNamespace(
        jumpto_transcript_mode=mode,
        jumpto_live_external_calls=live,
        assembly_api_key=api_key,
    )


class TestMediaInfoProvider:
    """Tests for the media info provider selection."""

    def test_no_live_calls_returns_fake_media_info(self, monkeypatch) -> None:
        settings = SimpleNamespace(jumpto_live_external_calls=False)
        monkeypatch.setattr("app.providers.media.get_settings", lambda: settings)

        info = get_media_info("abcde12345", "https://youtu.be/abcde12345")

        assert info.duration_seconds == 300
        assert "abcde12345" in info.title

    def test_live_provider_error_is_wrapped(self, monkeypatch) -> None:
        settings = SimpleNamespace(jumpto_live_external_calls=True)
        monkeypatch.setattr("app.providers.media.get_settings", lambda: settings)

        def boom(url: str):
            raise OSError("network down")

        monkeypatch.setattr("app.providers.media._fetch_from_yt_dlp", boom)

        with pytest.raises(ExternalServiceError):
            get_media_info("abcde12345", "https://youtu.be/abcde12345")

    def test_live_provider_external_error_propagates(self, monkeypatch) -> None:
        settings = SimpleNamespace(jumpto_live_external_calls=True)
        monkeypatch.setattr("app.providers.media.get_settings", lambda: settings)
        monkeypatch.setattr(
            "app.providers.media._fetch_from_yt_dlp",
            lambda url: (_ for _ in ()).throw(ExternalServiceError("up", service="yt-dlp")),
        )

        with pytest.raises(ExternalServiceError):
            get_media_info("abcde12345", "https://youtu.be/abcde12345")


class TestTranscriptProviderSelection:
    """Tests for transcript provider factory selection."""

    def test_fake_mode_wins_even_with_credentials(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.providers.transcript.get_settings", lambda: _FAKE_SETTINGS_FAKE_MODE
        )

        provider = get_transcript_provider()

        assert isinstance(provider, FakeTranscriptProvider)

    def test_falls_back_to_fake_when_live_not_configured(self, monkeypatch) -> None:
        settings = _settings(mode="real", live=False, api_key="")
        monkeypatch.setattr("app.providers.transcript.get_settings", lambda: settings)

        provider = get_transcript_provider()

        assert isinstance(provider, FakeTranscriptProvider)

    def test_returns_assembly_when_fully_configured(self, monkeypatch) -> None:
        settings = _settings(mode="real", live=True, api_key="key-123")
        monkeypatch.setattr("app.providers.transcript.get_settings", lambda: settings)

        provider = get_transcript_provider()

        assert isinstance(provider, AssemblyTranscriptProvider)
        assert provider.api_key == "key-123"


class TestFakeTranscriptProvider:
    """Tests for the deterministic fake transcript."""

    @pytest.mark.asyncio
    async def test_fetch_returns_timestamped_words(self) -> None:
        provider = FakeTranscriptProvider()

        transcript = await provider.fetch("https://youtu.be/abcde12345")

        assert transcript.language == "en"
        assert len(transcript.words) > 0
        assert transcript.words[0].start_time == 0.0
        assert transcript.text == " ".join(word.word for word in transcript.words)


class TestAssemblyParser:
    """Tests for Assembly.ai response parsing."""

    def test_parse_assembly_transcript(self) -> None:
        data = {
            "language_code": "en",
            "text": "hello world",
            "words": [
                {"text": "hello", "start": 100, "end": 250},
                {"text": "world", "start": 250, "end": 500},
            ],
        }

        parsed = _parse_assembly_transcript(data)

        assert parsed.language == "en"
        assert parsed.text == "hello world"
        assert parsed.words[0].start_time == 0.1
        assert parsed.words[1].end_time == 0.5


class TestAssemblyFetcher:
    """Tests for the Assembly.ai HTTP flow."""

    @pytest.mark.asyncio
    async def test_fetch_downloads_uploads_and_polls(self, monkeypatch, tmp_path) -> None:
        audio_file = tmp_path / "audio.webm"
        audio_file.write_bytes(b"fake-audio")

        monkeypatch.setattr("app.providers.transcript._download_audio", lambda url: str(audio_file))

        upload_response = Mock(status_code=200)
        upload_response.json.return_value = {"upload_url": "https://cdn.assemblyai.com/fake"}

        submit_response = Mock(status_code=200)
        submit_response.json.return_value = {"id": "transcript-1"}

        completed_body = {
            "status": "completed",
            "language_code": "en",
            "text": "hello world",
            "words": [{"text": "hello", "start": 0, "end": 100}],
        }
        poll_response = Mock(status_code=200)
        poll_response.json.return_value = completed_body

        client = AsyncMock()
        client.post.side_effect = [upload_response, submit_response]
        client.get.return_value = poll_response
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr("app.providers.transcript.httpx.AsyncClient", lambda: client)

        provider = AssemblyTranscriptProvider("key")
        transcript = await provider.fetch("https://youtu.be/abcde12345")

        assert transcript.text == "hello world"
        assert transcript.words[0].word == "hello"
        assert client.post.call_count == 2
        assert client.get.call_count >= 1
        assert not audio_file.exists()

    @pytest.mark.asyncio
    async def test_fetch_raises_when_upload_fails(self, monkeypatch, tmp_path) -> None:
        audio_file = tmp_path / "audio.webm"
        audio_file.write_bytes(b"fake-audio")
        monkeypatch.setattr("app.providers.transcript._download_audio", lambda url: str(audio_file))

        upload_response = Mock(status_code=400)
        upload_response.json.return_value = {"error": "bad"}

        client = AsyncMock()
        client.post.return_value = upload_response
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)

        monkeypatch.setattr("app.providers.transcript.httpx.AsyncClient", lambda: client)

        provider = AssemblyTranscriptProvider("key")
        with pytest.raises(ExternalServiceError):
            await provider.fetch("https://youtu.be/abcde12345")

    def test_download_audio_removes_file_on_failure(self, monkeypatch, tmp_path) -> None:
        from app.providers import transcript as transcript_module

        destination = str(tmp_path / "audio.webm")
        monkeypatch.setattr(
            "app.providers.transcript.tempfile.mkstemp", lambda *a, **k: (0, destination)
        )
        monkeypatch.setattr("app.providers.transcript.os.close", lambda fd: None)

        def boom(options: dict, url: str):
            raise OSError("download failed")

        monkeypatch.setattr("app.providers.transcript._run_download", boom)

        with pytest.raises(ExternalServiceError):
            transcript_module._download_audio("https://youtu.be/abcde12345")
        assert not transcript_module.os.path.exists(destination)


class TestAssemblyDownloadAudio:
    """Tests for the yt-dlp audio download helper."""

    def test_download_audio_success_returns_path(self, monkeypatch, tmp_path) -> None:
        from app.providers import transcript as transcript_module

        destination = str(tmp_path / "audio.webm")
        monkeypatch.setattr(
            "app.providers.transcript.tempfile.mkstemp", lambda *a, **k: (0, destination)
        )
        monkeypatch.setattr("app.providers.transcript.os.close", lambda fd: None)

        def fake_download(options: dict, url: str):
            from pathlib import Path

            Path(options["outtmpl"]).write_bytes(b"audio-bytes")

        monkeypatch.setattr("app.providers.transcript._run_download", fake_download)

        path = transcript_module._download_audio("https://youtu.be/abcde12345")

        assert path == destination
        assert transcript_module.os.path.getsize(destination) > 0

    def test_download_audio_raises_when_no_file(self, monkeypatch, tmp_path) -> None:
        from app.providers import transcript as transcript_module

        destination = str(tmp_path / "audio.webm")
        monkeypatch.setattr(
            "app.providers.transcript.tempfile.mkstemp", lambda *a, **k: (0, destination)
        )
        monkeypatch.setattr("app.providers.transcript.os.close", lambda fd: None)
        monkeypatch.setattr("app.providers.transcript._run_download", lambda options, url: None)

        with pytest.raises(ExternalServiceError):
            transcript_module._download_audio("https://youtu.be/abcde12345")
