"""Ensure evaluation-run persistence exists.

Revision ID: 0005_evaluations
Revises: 0004_measurement
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_evaluations"
down_revision = "0004_measurement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "eval_runs" not in inspector.get_table_names():
        op.create_table(
            "eval_runs",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("suite", sa.String(), nullable=False),
            sa.Column("prompt_version", sa.String()),
            sa.Column("model", sa.String()),
            sa.Column("metrics", sa.JSON(), nullable=False),
            sa.Column("passed", sa.Boolean(), nullable=False),
            sa.Column("ran_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    # eval_runs was part of the initial metadata on newer installs. Dropping it here
    # would destroy results that predate this compatibility migration.
    pass
