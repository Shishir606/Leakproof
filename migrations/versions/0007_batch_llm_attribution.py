"""Attribute aggregate model calls to a batch run.

Revision ID: 0007_batch_llm_attribution
Revises: 0006_voice_promises
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_batch_llm_attribution"
down_revision = "0006_voice_promises"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("llm_calls")}
    if "batch_run_id" not in columns:
        op.add_column("llm_calls", sa.Column("batch_run_id", sa.String(), nullable=True))
        op.create_foreign_key(
            "fk_llm_calls_batch_run_id",
            "llm_calls",
            "batch_runs",
            ["batch_run_id"],
            ["id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("llm_calls")}
    if "batch_run_id" in columns:
        matching_foreign_keys = [
            foreign_key
            for foreign_key in inspector.get_foreign_keys("llm_calls")
            if foreign_key.get("constrained_columns") == ["batch_run_id"]
        ]
        for foreign_key in matching_foreign_keys:
            if foreign_key.get("name"):
                op.drop_constraint(
                    foreign_key["name"], "llm_calls", type_="foreignkey"
                )
        op.drop_column("llm_calls", "batch_run_id")
