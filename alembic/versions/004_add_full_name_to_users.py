"""add users.full_name

The display name Google returns on sign-in, so the interface can greet an
account by name instead of by address. Nullable: a password-only account has
no such name, and an existing Google account has none until it signs in
again. Nothing is backfilled, so every row starts null.

Revision ID: 004
Revises: 003
Create Date: 2026-10-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the display name column."""
    op.add_column("users", sa.Column("full_name", sa.String(length=255), nullable=True))


def downgrade() -> None:
    """Drop the display name column."""
    op.drop_column("users", "full_name")
