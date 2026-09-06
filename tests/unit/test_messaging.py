"""Unit tests for the Celery messaging client."""

from uuid import UUID

from app.services import messaging


class TestDispatchTranscription:
    """Tests for publishing transcription jobs to the worker queue."""

    def test_sends_worker_task_with_job_id(self, monkeypatch) -> None:
        sent = []
        monkeypatch.setattr(
            messaging.celery_app,
            "send_task",
            lambda name, args=None, **kwargs: sent.append((name, args)),
        )

        messaging.dispatch_transcription(UUID("12345678-1234-5678-1234-567812345678"))

        assert sent == [
            (
                "app.tasks.transcription.download_and_transcribe",
                ["12345678-1234-5678-1234-567812345678"],
            )
        ]


class TestCeleryAppConfiguration:
    """Tests for the Celery app configuration."""

    def test_uses_json_serialization(self) -> None:
        assert messaging.celery_app.conf.task_serializer == "json"
        assert messaging.celery_app.conf.accept_content == ["json"]
        assert messaging.celery_app.conf.result_serializer == "json"

    def test_tracks_no_result_backend(self) -> None:
        assert messaging.celery_app.conf.result_backend is None
