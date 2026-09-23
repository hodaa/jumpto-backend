"""widen transcript word column to 500

Revision ID: 95a988405d1d
Revises: f4a1c0552bc7
Create Date: 2026-09-23 17:30:17.091263

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "95a988405d1d"
down_revision: str | None = "f4a1c0552bc7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen transcript word column so long caption tokens fit."""
    op.alter_column(
        "transcript_words",
        "word",
        existing_type=sa.VARCHAR(length=100),
        type_=sa.String(length=500),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Restore the original word column width."""
    op.alter_column(
        "transcript_words",
        "word",
        existing_type=sa.String(length=500),
        type_=sa.VARCHAR(length=100),
        existing_nullable=False,
    )
