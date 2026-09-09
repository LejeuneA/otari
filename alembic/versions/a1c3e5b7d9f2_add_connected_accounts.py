"""Add end users, connected accounts and their pending OAuth state.

``end_users`` names the users of an application built on this deployment, as
that application names them (the ``user`` field on a request), scoped to the
workspace its API key belongs to. ``connected_accounts`` holds the third-party
accounts connected through OAuth (``docs/connections.md``): one row per owner,
provider, application-defined key and account identifier, with the tokens
encrypted under the row's own key, which is itself encrypted with
``OTARI_SECRET_KEY``. An owner is either an end user or, for a credential a
whole team acts through, the workspace — exactly one of the two columns is set.
``connected_account_oauth_states`` is the pending half of a flow between the
consent screen and the callback, persisted so a callback may land on any
worker; rows are single-use through ``consumed_at`` and carry the outcome so an
application can ask how a flow ended without holding the browser's ``state``.

All of it cascades downwards: a workspace's end users and shared grants, and an
end user's own grants and pending flows, have no meaning once their parent is
gone.

Revision ID: a1c3e5b7d9f2
Revises: d5b7f9a1c3e6
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5b7d9f2"
down_revision: str | Sequence[str] | None = "d5b7f9a1c3e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNIQUE_END_USER = "uq_end_users_workspace_external_id"
_UNIQUE_ACCOUNT_USER = "uq_connected_accounts_end_user_provider_key_account"
_UNIQUE_ACCOUNT_WORKSPACE = "uq_connected_accounts_workspace_provider_key_account"
_ONE_OWNER = "ck_connected_accounts_one_owner"


def upgrade() -> None:
    op.create_table(
        "end_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("workspace_id", "external_id", name=_UNIQUE_END_USER),
    )
    op.create_index(op.f("ix_end_users_workspace_id"), "end_users", ["workspace_id"])

    op.create_table(
        "connected_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("end_user_id", sa.Uuid(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("connected_by_end_user_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("connection_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("account_identifier", sa.String(length=320), nullable=True),
        sa.Column("account_label", sa.String(length=200), nullable=True),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("invalid_reason", sa.String(length=200), nullable=True),
        sa.Column("invalid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("encrypted_extra_tokens", sa.Text(), nullable=True),
        sa.Column("encrypted_data_key", sa.Text(), nullable=True),
        sa.Column("token_type", sa.String(length=50), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("extra_scopes", sa.JSON(), nullable=True),
        sa.Column("account_metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["end_user_id"], ["end_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connected_by_end_user_id"], ["end_users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "end_user_id", "provider", "connection_key", "account_identifier", name=_UNIQUE_ACCOUNT_USER
        ),
        sa.UniqueConstraint(
            "workspace_id", "provider", "connection_key", "account_identifier", name=_UNIQUE_ACCOUNT_WORKSPACE
        ),
        sa.CheckConstraint("(end_user_id IS NULL) <> (workspace_id IS NULL)", name=_ONE_OWNER),
    )
    op.create_index(op.f("ix_connected_accounts_end_user_id"), "connected_accounts", ["end_user_id"])
    op.create_index(op.f("ix_connected_accounts_workspace_id"), "connected_accounts", ["workspace_id"])
    op.create_index(op.f("ix_connected_accounts_provider"), "connected_accounts", ["provider"])

    op.create_table(
        "connected_account_oauth_states",
        sa.Column("state", sa.String(length=128), nullable=False),
        sa.Column("flow_id", sa.Uuid(), nullable=False),
        sa.Column("end_user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("connection_key", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expected_account_identifier", sa.String(length=320), nullable=True),
        sa.Column("redirect_uri", sa.Text(), nullable=False),
        sa.Column("return_url", sa.Text(), nullable=True),
        sa.Column("encrypted_code_verifier", sa.Text(), nullable=True),
        sa.Column("requested_scopes", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("connected_account_id", sa.Uuid(), nullable=True),
        sa.Column("failure_reason", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("state"),
        sa.ForeignKeyConstraint(["end_user_id"], ["end_users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["connected_account_id"], ["connected_accounts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("flow_id", name="uq_connected_account_oauth_states_flow_id"),
    )
    op.create_index(
        op.f("ix_connected_account_oauth_states_end_user_id"), "connected_account_oauth_states", ["end_user_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_connected_account_oauth_states_end_user_id"), table_name="connected_account_oauth_states")
    op.drop_table("connected_account_oauth_states")
    op.drop_index(op.f("ix_connected_accounts_provider"), table_name="connected_accounts")
    op.drop_index(op.f("ix_connected_accounts_workspace_id"), table_name="connected_accounts")
    op.drop_index(op.f("ix_connected_accounts_end_user_id"), table_name="connected_accounts")
    op.drop_table("connected_accounts")
    op.drop_index(op.f("ix_end_users_workspace_id"), table_name="end_users")
    op.drop_table("end_users")
