"""Add durable actuator execution state.

Revision ID: 0003_action_execution
Revises: 0002_append_only_events
Create Date: 2026-08-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0003_action_execution"
down_revision = "0002_append_only_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 mirrors current ORM metadata, so these guards support both databases
    # created before this slice and fresh installs created after it.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    action_columns = {column["name"] for column in inspector.get_columns("actions")}
    if "attempt_count" not in action_columns:
        op.add_column(
            "actions",
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        )
        op.alter_column("actions", "attempt_count", server_default=None)
    if "actuator_receipts" not in inspector.get_table_names():
        op.create_table(
            "actuator_receipts",
            sa.Column("idempotency_key", sa.String(), primary_key=True),
            sa.Column("action_id", sa.String(), sa.ForeignKey("actions.id"), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("provider_ref", sa.String(), nullable=False),
            sa.Column("request", sa.JSON(), nullable=False),
            sa.Column("response", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "actuator_receipts" in inspector.get_table_names():
        op.drop_table("actuator_receipts")
    action_columns = {column["name"] for column in inspector.get_columns("actions")}
    if "attempt_count" in action_columns:
        op.drop_column("actions", "attempt_count")
