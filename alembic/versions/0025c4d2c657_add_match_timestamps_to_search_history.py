"""add_match_timestamps_to_search_history

Revision ID: 0025c4d2c657
Revises: 005
Create Date: 2026-10-02 19:23:36.434278

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '0025c4d2c657'
down_revision: Union[str, None] = '005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "search_history",
        sa.Column("match_timestamps", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("search_history", "match_timestamps")
