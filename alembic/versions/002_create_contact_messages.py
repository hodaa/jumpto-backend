"""Create contact_messages table

Revision ID: 002
Revises: 95a988405d1d
Create Date: 2026-09-25 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: str | None = "95a988405d1d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_contact_messages_created_at",
        "contact_messages",
        ["created_at"],
    )
    op.create_index(
        "idx_contact_messages_email",
        "contact_messages",
        ["email"],
    )


def downgrade() -> None:
    op.drop_index("idx_contact_messages_email", table_name="contact_messages")
    op.drop_index("idx_contact_messages_created_at", table_name="contact_messages")
    op.drop_table("contact_messages")
