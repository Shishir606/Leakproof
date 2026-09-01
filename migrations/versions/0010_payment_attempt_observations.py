"""Add sanitized payment-attempt observations.

Revision ID: 0010_payment_attempts
Revises: 0009_resend_delivery_guard
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0010_payment_attempts"
down_revision = "0009_resend_delivery_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "llm_calls" in tables:
        columns = {item["name"] for item in inspector.get_columns("llm_calls")}
        with op.batch_alter_table("llm_calls") as batch:
            if "provider" not in columns:
                batch.add_column(
                    sa.Column(
                        "provider", sa.String(length=40), nullable=False, server_default="unknown"
                    )
                )
            if "merchant_id" not in columns:
                batch.add_column(
                    sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"))
                )
            if "request_id" not in columns:
                batch.add_column(sa.Column("request_id", sa.String(length=255)))
            if "error_class" not in columns:
                batch.add_column(sa.Column("error_class", sa.String(length=100)))
        op.execute(
            """
            UPDATE llm_calls
            SET merchant_id = (
                SELECT cases.merchant_id FROM cases WHERE cases.id = llm_calls.case_id
            )
            WHERE merchant_id IS NULL AND case_id IS NOT NULL
            """
        )
        op.execute(
            """
            UPDATE llm_calls
            SET merchant_id = (
                SELECT batch_runs.merchant_id
                FROM batch_runs
                WHERE batch_runs.id = llm_calls.batch_run_id
            )
            WHERE merchant_id IS NULL AND batch_run_id IS NOT NULL
            """
        )
    if "payment_attempt_observations" in tables:
        return
    op.create_table(
        "payment_attempt_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("namespace", sa.String(length=160), nullable=False),
        sa.Column("attempt_key", sa.String(length=255), nullable=False),
        sa.Column("provider_event_key", sa.String(length=255), nullable=False),
        sa.Column("provider_payment_id", sa.String(length=255)),
        sa.Column("provider_order_id", sa.String(length=255)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=80), nullable=False),
        sa.Column("issuer", sa.String(length=120), nullable=False),
        sa.Column("bin_bucket", sa.String(length=32), nullable=False),
        sa.Column("checkout_step", sa.String(length=120), nullable=False),
        sa.Column("checkout_version", sa.String(length=80), nullable=False),
        sa.Column("error_reason", sa.String(length=160), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "merchant_id",
            "provider",
            "namespace",
            "attempt_key",
            name="uq_payment_attempt_provider_key",
        ),
    )
    op.create_index(
        "ix_payment_attempt_merchant_observed",
        "payment_attempt_observations",
        ["merchant_id", "namespace", "observed_at"],
    )
    op.create_index(
        "ix_payment_attempt_outcome",
        "payment_attempt_observations",
        ["merchant_id", "namespace", "outcome"],
    )
    op.create_index(
        "ix_payment_attempt_issuer",
        "payment_attempt_observations",
        ["merchant_id", "namespace", "issuer"],
    )
    op.create_index(
        "ix_payment_attempt_method",
        "payment_attempt_observations",
        ["merchant_id", "namespace", "method"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "payment_attempt_observations" in tables:
        op.drop_table("payment_attempt_observations")
    if "llm_calls" in tables:
        columns = {item["name"] for item in inspector.get_columns("llm_calls")}
        with op.batch_alter_table("llm_calls") as batch:
            for name in ("error_class", "request_id", "provider", "merchant_id"):
                if name in columns:
                    batch.drop_column(name)
