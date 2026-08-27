"""Add deterministic measurement and attribution state.

Revision ID: 0004_measurement
Revises: 0003_action_execution
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_measurement"
down_revision = "0003_action_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    case_columns = {column["name"] for column in inspector.get_columns("cases")}

    if "batch_runs" not in tables:
        op.create_table(
            "batch_runs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
            sa.Column("holdout_seed", sa.Integer(), nullable=False),
            sa.Column("holdout_fraction", sa.Numeric(5, 4), nullable=False),
            sa.Column("measurement_config", sa.JSON(), nullable=False),
        )
    if "batch_run_id" not in case_columns:
        op.add_column("cases", sa.Column("batch_run_id", sa.String()))
        op.create_index("ix_cases_batch_run", "cases", ["batch_run_id"])
    if "amount_band" not in case_columns:
        op.add_column(
            "cases",
            sa.Column("amount_band", sa.String(), nullable=False, server_default="UNASSIGNED"),
        )
        op.alter_column("cases", "amount_band", server_default=None)
    if "recovery_attributions" not in tables:
        op.create_table(
            "recovery_attributions",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("payment_entity_id", sa.String(), nullable=False),
            sa.Column("amount_paise", sa.BigInteger(), nullable=False),
            sa.Column("matched_by", sa.String(), nullable=False),
            sa.Column("credit_rule", sa.String(), nullable=False),
            sa.Column("credited_action_id", sa.String(), sa.ForeignKey("actions.id")),
            sa.Column("credited_action_type", sa.String()),
            sa.Column("touch_at", sa.DateTime(timezone=True)),
            sa.Column("organic", sa.Boolean(), nullable=False),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attributed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("case_id", name="uq_attribution_case"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "recovery_attributions" in tables:
        op.drop_table("recovery_attributions")
    case_columns = {column["name"] for column in inspector.get_columns("cases")}
    if "amount_band" in case_columns:
        op.drop_column("cases", "amount_band")
    if "batch_run_id" in case_columns:
        op.drop_index("ix_cases_batch_run", table_name="cases")
        op.drop_column("cases", "batch_run_id")
    if "batch_runs" in tables:
        op.drop_table("batch_runs")
