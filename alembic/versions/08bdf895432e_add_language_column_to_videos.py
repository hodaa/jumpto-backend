"""add language column to videos

Revision ID: 08bdf895432e
Revises: 001
Create Date: 2026-08-31 09:20:55.349373

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "08bdf895432e"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the language column to the videos table."""
    op.add_column("videos", sa.Column("language", sa.String(length=20), nullable=True))


def downgrade() -> None:
    """Drop the language column from the videos table."""
    op.drop_column("videos", "language")
