"""Add live-demo sessions, telemetry, and provider state.

Revision ID: 0008_live_demo_contracts
Revises: 0007_batch_llm_attribution
Create Date: 2026-08-29
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_live_demo_contracts"
down_revision = "0007_batch_llm_attribution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    session_state = sa.Enum(
        "CREATED",
        "CHECKOUT_OPEN",
        "AT_RISK",
        "RECOVERED",
        "EXPIRED",
        name="demo_session_state",
    )

    if "demo_sessions" not in tables:
        op.create_table(
            "demo_sessions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"), nullable=False),
            sa.Column("customer_id", sa.String(), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("razorpay_order_id", sa.String(), nullable=False),
            sa.Column("amount_paise", sa.BigInteger(), nullable=False),
            sa.Column("currency", sa.String(length=3), nullable=False),
            sa.Column("state", session_state, nullable=False),
            sa.Column("recipient_ciphertext", sa.Text()),
            sa.Column("recipient_hash", sa.String(length=64)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "razorpay_order_id", name="uq_demo_sessions_razorpay_order"
            ),
        )
        op.create_index(
            "ix_demo_sessions_merchant_state", "demo_sessions", ["merchant_id", "state"]
        )
        op.create_index("ix_demo_sessions_expires", "demo_sessions", ["expires_at"])
        op.create_index(
            "ix_demo_sessions_recipient_hash", "demo_sessions", ["recipient_hash"]
        )

    if "checkout_events" not in tables:
        op.create_table(
            "checkout_events",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "session_id", sa.String(), sa.ForeignKey("demo_sessions.id"), nullable=False
            ),
            sa.Column("client_event_id", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=40), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metadata", sa.JSON(), nullable=False),
            sa.UniqueConstraint(
                "session_id", "client_event_id", name="uq_checkout_events_session_client"
            ),
        )
        op.create_index(
            "ix_checkout_events_session_received",
            "checkout_events",
            ["session_id", "received_at"],
        )

    if "provider_calls" not in tables:
        op.create_table(
            "provider_calls",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("session_id", sa.String(), sa.ForeignKey("demo_sessions.id")),
            sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id")),
            sa.Column("action_id", sa.String(), sa.ForeignKey("actions.id")),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("operation", sa.String(length=80), nullable=False),
            sa.Column("request_id", sa.String(length=255)),
            sa.Column("safe_response_metadata", sa.JSON(), nullable=False),
            sa.Column("latency_ms", sa.Integer(), nullable=False),
            sa.Column("attempt_number", sa.SmallInteger(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("error_class", sa.String(length=100)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(
            "ix_provider_calls_session", "provider_calls", ["session_id", "created_at"]
        )
        op.create_index("ix_provider_calls_case", "provider_calls", ["case_id", "created_at"])
        op.create_index(
            "ix_provider_calls_provider_status", "provider_calls", ["provider", "status"]
        )

    if "email_deliveries" not in tables:
        op.create_table(
            "email_deliveries",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column(
                "session_id", sa.String(), sa.ForeignKey("demo_sessions.id"), nullable=False
            ),
            sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("action_id", sa.String(), sa.ForeignKey("actions.id"), nullable=False),
            sa.Column("provider_email_id", sa.String(length=128)),
            sa.Column("recipient_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("action_id", name="uq_email_deliveries_action"),
            sa.UniqueConstraint(
                "provider_email_id", name="uq_email_deliveries_provider_email"
            ),
        )
        op.create_index(
            "ix_email_deliveries_recipient_created",
            "email_deliveries",
            ["recipient_hash", "created_at"],
        )

    if "email_delivery_events" not in tables:
        op.create_table(
            "email_delivery_events",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("provider_email_id", sa.String(length=128), nullable=False),
            sa.Column("provider_event_id", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=40), nullable=False),
            sa.Column("safe_payload", sa.JSON(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "provider_email_id",
                "provider_event_id",
                name="uq_email_delivery_provider_event",
            ),
        )
        op.create_index(
            "ix_email_delivery_events_email_created",
            "email_delivery_events",
            ["provider_email_id", "created_at"],
        )

    if "case_insights" not in tables:
        op.create_table(
            "case_insights",
            sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), primary_key=True),
            sa.Column("summary", sa.String(length=500)),
            sa.Column("probable_cause", sa.String(length=500)),
            sa.Column("evidence", sa.JSON(), nullable=False),
            sa.Column("recommended_next_step", sa.String(length=500)),
            sa.Column("confidence", sa.Numeric(4, 3)),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("fallback_reason", sa.String(length=100)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "confidence >= 0 AND confidence <= 1", name="ck_case_insight_confidence"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    for table in (
        "case_insights",
        "email_delivery_events",
        "email_deliveries",
        "provider_calls",
        "checkout_events",
        "demo_sessions",
    ):
        if table in tables:
            op.drop_table(table)

    if bind.dialect.name == "postgresql":
        sa.Enum(name="demo_session_state").drop(bind, checkfirst=True)
