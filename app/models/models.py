"""SQLAlchemy models for the application."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Video(Base):
    """Video model representing a YouTube video."""

    __tablename__ = "videos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    youtube_url: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    video_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(20), nullable=True)
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_tsvector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    transcribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # Relationships
    transcript_words: Mapped[list["TranscriptWord"]] = relationship(
        "TranscriptWord",
        back_populates="video",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    jobs: Mapped[list["Job"]] = relationship(
        "Job",
        back_populates="video",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        Index("idx_videos_transcript_tsvector", "transcript_tsvector", postgresql_using="gin"),
    )

    def __repr__(self) -> str:
        return f"<Video(id={self.id}, video_id={self.video_id}, title={self.title})>"


class TranscriptWord(Base):
    """Transcript word model for exact phrase search."""

    __tablename__ = "transcript_words"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    word_index: Mapped[int] = mapped_column(Integer, nullable=False)
    word: Mapped[str] = mapped_column(String(100), nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)

    # Relationships
    video: Mapped["Video"] = relationship("Video", back_populates="transcript_words")

    __table_args__ = (
        Index("idx_transcript_words_video_word", "video_id", "word"),
        UniqueConstraint("video_id", "word_index", name="uq_transcript_words_video_index"),
    )

    def __repr__(self) -> str:
        return (
            f"<TranscriptWord(video_id={self.video_id}, word={self.word}, index={self.word_index})>"
        )


class Job(Base):
    """Job model for tracking transcription jobs."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    video_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("videos.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    progress: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # Relationships
    video: Mapped["Video"] = relationship("Video", back_populates="jobs")

    __table_args__ = (
        # Unique partial index: only one in-flight job per video
        Index(
            "uq_jobs_video_in_flight",
            "video_id",
            unique=True,
            postgresql_where="status IN ('pending', 'processing')",
        ),
        Index("idx_jobs_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<Job(id={self.id}, video_id={self.video_id}, status={self.status})>"
