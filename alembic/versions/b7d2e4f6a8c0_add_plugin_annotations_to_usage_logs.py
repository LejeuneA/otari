"""add plugin_annotations to usage_logs

Revision ID: b7d2e4f6a8c0
Revises: a1d4f7c2e8b3
Create Date: 2026-09-10

What plugin traffic observers annotated on a request, keyed by plugin name.
Opaque JSON; NULL when no observer said anything.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d2e4f6a8c0"
down_revision = "a1d4f7c2e8b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("usage_logs", sa.Column("plugin_annotations", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_logs", "plugin_annotations")
