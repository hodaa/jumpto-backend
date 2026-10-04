"""add search_history.progress_seconds and search_history.video_title

So a saved search can be replayed where it landed. Both are snapshots taken when
the search ran, deliberately not joins to the live video row:

* progress_seconds is where the first match sat, so "open this search" resumes
  at the moment the visitor was reading rather than at 00:00. Nullable, because
  a search recorded while the transcript was still being built has no match yet
  and an existing row predates the column.
* video_title is copied in for the same reason the timestamp is a snapshot:
  the history page has to keep saying what the video was called when the search
  was made, and the videos row is the thing that can be re-fetched and renamed
  later. It is also what lets the page fall back to a name instead of a bare
  video id.

Nothing is backfilled. Old rows keep a null timestamp, and the interface falls
back to the start of the video for those, which is what opening an untimed
search should do.

Revision ID: 005
Revises: 004
Create Date: 2026-10-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the replay position and the video name to the history row."""
    op.add_column(
        "search_history",
        sa.Column("progress_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "search_history",
        sa.Column("video_title", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    """Drop both columns."""
    op.drop_column("search_history", "video_title")
    op.drop_column("search_history", "progress_seconds")