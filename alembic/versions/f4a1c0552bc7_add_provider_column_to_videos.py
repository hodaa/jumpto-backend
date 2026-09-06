"""add provider column to videos

Revision ID: f4a1c0552bc7
Revises: 08bdf895432e
Create Date: 2026-09-05 17:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a1c0552bc7"
down_revision: str | None = "08bdf895432e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the provider column to the videos table."""
    op.add_column(
        "videos",
        sa.Column(
            "provider",
            sa.String(length=50),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    """Drop the provider column from the videos table."""
    op.drop_column("videos", "provider")
