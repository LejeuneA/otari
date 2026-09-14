"""Add guardrail_credentials table.

Revision ID: a7e3c9d1f5b2
Revises: f1c4a8e2d6b9
Create Date: 2026-09-14 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e3c9d1f5b2"
down_revision: str | Sequence[str] | None = "f1c4a8e2d6b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "guardrail_credentials",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("guardrail_name", sa.String(), nullable=False),
        sa.Column("create_kwargs", sa.JSON(), nullable=False),
        sa.Column("encrypted_create_secrets", sa.Text(), nullable=True),
        sa.Column("validate_kwargs", sa.JSON(), nullable=False),
        # Server default so the column is non-null for any row written by code
        # that predates it; there are none today, but the rule is the repo's.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("guardrail_credentials")
