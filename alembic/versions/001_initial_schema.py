"""Initial schema

Revision ID: 001
Revises: 
Create Date: 2024-01-01 00:00:00

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create videos table
    op.create_table(
        "videos",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("youtube_url", sa.String(500), nullable=False),
        sa.Column("video_id", sa.String(50), nullable=False),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("transcript_tsvector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("transcribed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("youtube_url"),
        sa.UniqueConstraint("video_id"),
    )

    # Create index on video_id for fast lookups
    op.create_index("ix_videos_video_id", "videos", ["video_id"], unique=False)

    # Create GIN index on transcript_tsvector for full-text search
    op.execute(
        "CREATE INDEX idx_videos_transcript_tsvector ON videos USING GIN (transcript_tsvector)"
    )

    # Create transcript_words table
    op.create_table(
        "transcript_words",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("word_index", sa.Integer(), nullable=False),
        sa.Column("word", sa.String(100), nullable=False),
        sa.Column("start_time", sa.Float(), nullable=False),
        sa.Column("end_time", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
    )

    # Create index on (video_id, word) for search
    op.create_index(
        "idx_transcript_words_video_word", "transcript_words", ["video_id", "word"], unique=False
    )

    # Create unique constraint on (video_id, word_index)
    op.create_unique_constraint(
        "uq_transcript_words_video_index", "transcript_words", ["video_id", "word_index"]
    )

    # Create jobs table
    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("video_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"], ondelete="CASCADE"),
    )

    # Create index on status
    op.create_index("idx_jobs_status", "jobs", ["status"], unique=False)

    # Create unique partial index for in-flight jobs (one per video)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_jobs_video_in_flight 
        ON jobs (video_id) 
        WHERE status IN ('pending', 'processing')
    """
    )


def downgrade() -> None:
    # Drop unique partial index
    op.execute("DROP INDEX IF EXISTS uq_jobs_video_in_flight")

    # Drop jobs table
    op.drop_index("idx_jobs_status", table_name="jobs")
    op.drop_table("jobs")

    # Drop transcript_words table
    op.drop_constraint("uq_transcript_words_video_index", "transcript_words", type_="unique")
    op.drop_index("idx_transcript_words_video_word", table_name="transcript_words")
    op.drop_table("transcript_words")

    # Drop videos table
    op.execute("DROP INDEX IF EXISTS idx_videos_transcript_tsvector")
    op.drop_index("ix_videos_video_id", table_name="videos")
    op.drop_table("videos")
