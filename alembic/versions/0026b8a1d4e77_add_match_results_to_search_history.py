"""add_match_results_to_search_history

Revision ID: 0026b8a1d4e77
Revises: 0025c4d2c657
Create Date: 2026-10-03 10:14:02.118904

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "0026b8a1d4e77"
down_revision: Union[str, None] = "0025c4d2c657"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Store the whole result set, not just where it sat.

    ``match_timestamps`` answers only "at which seconds did this match", which
    is enough to seek but not enough to show the visitor what was found. The
    full snapshot - timestamp, progress and the snippet around the phrase - is
    what the search returned at the time, and replaying a saved entry should
    show the same list the search produced.

    Left nullable: rows written before this column, and rows still waiting on a
    transcript, have no snapshot to store.
    """
    op.add_column(
        "search_history",
        sa.Column("match_results", postgresql.JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("search_history", "match_results")
