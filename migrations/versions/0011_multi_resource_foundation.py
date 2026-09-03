"""Generalized sessions, provider correlation and one ledger per obligation.

Revision ID: 0011_multi_resource
Revises: 0010_payment_attempts
"""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_multi_resource"
down_revision = "0010_payment_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {item["name"] for item in inspector.get_columns("demo_sessions")}
    # 0001 historically creates current metadata. Fresh installs already have these
    # tables; an actual 0010 database takes the explicit additive/backfill path.
    if "primary_entity_id" not in columns:
        with op.batch_alter_table("demo_sessions") as batch:
            batch.add_column(
                sa.Column(
                    "scenario_type", sa.String(), nullable=False, server_default="PAYMENT_FAILURE"
                )
            )
            batch.add_column(
                sa.Column(
                    "primary_entity_type", sa.String(), nullable=False, server_default="order"
                )
            )
            batch.add_column(sa.Column("primary_entity_id", sa.String(), nullable=True))
            batch.add_column(
                sa.Column("provider_mode", sa.String(), nullable=False, server_default="test")
            )
            batch.add_column(
                sa.Column("setup_state", sa.String(), nullable=False, server_default="READY")
            )
            # Historical mode/evidence cannot be inferred from the deployment's current mode.
            batch.add_column(
                sa.Column(
                    "capability_evidence",
                    sa.String(),
                    nullable=False,
                    server_default="ARCHITECTURE_READY",
                )
            )
        op.execute("UPDATE demo_sessions SET primary_entity_id = razorpay_order_id")
        with op.batch_alter_table("demo_sessions") as batch:
            batch.alter_column("primary_entity_id", existing_type=sa.String(), nullable=False)
            batch.alter_column("razorpay_order_id", existing_type=sa.String(), nullable=True)
            batch.drop_constraint("uq_demo_sessions_razorpay_order", type_="unique")
            batch.create_unique_constraint(
                "uq_demo_sessions_razorpay_order",
                ["merchant_id", "provider_mode", "razorpay_order_id"],
            )
            batch.create_unique_constraint(
                "uq_demo_session_scope", ["id", "merchant_id", "provider_mode"]
            )
            batch.create_check_constraint(
                "ck_demo_primary_type", "primary_entity_type IN ('order','invoice','subscription')"
            )
            batch.create_check_constraint(
                "ck_demo_provider_mode", "provider_mode IN ('test','live')"
            )
            batch.create_check_constraint(
                "ck_demo_setup_state",
                "setup_state IN ('CREATING','READY','ACTION_REQUIRED','FAILED','EXPIRED')",
            )
    if "primary_entity_id" not in columns:
        with op.batch_alter_table("cases") as batch:
            batch.create_unique_constraint("uq_cases_id_merchant", ["id", "merchant_id"])
        with op.batch_alter_table("demo_sessions") as batch:
            batch.create_check_constraint(
                "ck_demo_scenario",
                "scenario_type IN ('PAYMENT_FAILURE','CHECKOUT_ABANDON','INVOICE_OVERDUE',"
                "'SUBSCRIPTION_HALT','MANDATE_BROKEN')",
            )
            batch.create_check_constraint(
                "ck_demo_order_compatibility",
                "primary_entity_type != 'order' OR "
                "(razorpay_order_id IS NOT NULL AND razorpay_order_id=primary_entity_id)",
            )
    if "provider_obligations" not in set(inspector.get_table_names()):
        _create_tables()
    _backfill(bind)


def _backfill(bind) -> None:
    sessions = bind.execute(sa.text("SELECT * FROM demo_sessions")).mappings().all()
    for demo in sessions:
        if demo["primary_entity_type"] != "order":
            continue
        parts = [
            demo["merchant_id"],
            "razorpay",
            demo["provider_mode"],
            "order",
            demo["primary_entity_id"],
        ]
        identity = (
            "obl_" + hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()
        )
        if bind.execute(
            sa.text("SELECT id FROM provider_obligations WHERE id=:id"), {"id": identity}
        ).first():
            continue
        # Only proven legacy session/order or order-root bindings, never customer/amount.
        cases = (
            bind.execute(
                sa.text(
                    "SELECT * FROM cases WHERE merchant_id=:merchant AND batch_run_id IS NULL AND "
                    "(dedupe_key=:live_key OR dedupe_key=:payment_key OR "
                    "(entity_type='order' AND entity_id=:entity))"
                ),
                {
                    "merchant": demo["merchant_id"],
                    "entity": demo["primary_entity_id"],
                    "live_key": f"live:{demo['id']}:{demo['primary_entity_id']}",
                    "payment_key": f"pf:{demo['customer_id']}:{demo['primary_entity_id']}",
                },
            )
            .mappings()
            .all()
        )
        case = cases[0] if len(cases) == 1 else None
        credit = 0
        if case:
            credit = bind.execute(
                sa.text(
                    "SELECT COALESCE(SUM(amount_paise),0) FROM recovery_attributions "
                    "WHERE case_id=:id"
                ),
                {"id": case["id"]},
            ).scalar_one()
        bind.execute(
            sa.text(
                "INSERT INTO provider_obligations (id,merchant_id,provider,mode,entity_type,"
                "provider_entity_id,case_id,currency,baseline_paid_paise,detected_due_paise,"
                "outstanding_paise,recovered_paise,detected_at,settled_at,reconciliation_required) "
                "VALUES (:id,:merchant,'razorpay',:mode,'order',:entity,:case_id,:currency,0,:due,"
                ":outstanding,:credit,:detected,:settled,:review)"
            ),
            {
                "id": identity,
                "merchant": demo["merchant_id"],
                "mode": demo["provider_mode"],
                "entity": demo["primary_entity_id"],
                "case_id": case["id"] if case else None,
                "currency": demo["currency"],
                "due": max(case["amount_at_risk"], credit) if case else None,
                "outstanding": 0 if demo["state"] == "RECOVERED" else demo["amount_paise"],
                "credit": credit,
                "detected": case["detected_at"] if case else None,
                "settled": demo["updated_at"] if demo["state"] == "RECOVERED" else None,
                "review": len(cases) > 1,
            },
        )
        if case:
            attribution = (
                bind.execute(
                    sa.text("SELECT * FROM recovery_attributions WHERE case_id=:id"),
                    {"id": case["id"]},
                )
                .mappings()
                .first()
            )
            if attribution and attribution["payment_entity_id"].startswith("pay_"):
                existing = bind.execute(
                    sa.text(
                        "SELECT obligation_id FROM provider_settlements "
                        "WHERE merchant_id=:merchant "
                        "AND provider='razorpay' AND mode=:mode AND payment_id=:payment"
                    ),
                    {
                        "merchant": demo["merchant_id"],
                        "mode": demo["provider_mode"],
                        "payment": attribution["payment_entity_id"],
                    },
                ).scalar()
                if existing and existing != identity:
                    bind.execute(
                        sa.text(
                            "UPDATE provider_obligations SET reconciliation_required=true "
                            "WHERE id IN (:old,:new)"
                        ),
                        {"old": existing, "new": identity},
                    )
                elif not existing:
                    bind.execute(
                        sa.text(
                            "INSERT INTO provider_settlements "
                            "(merchant_id,provider,mode,payment_id,"
                            "obligation_id,amount_paise,credited_paise,currency,occurred_at,"
                            "credited_action_id,organic) VALUES "
                            "(:merchant,'razorpay',:mode,:payment,"
                            ":obligation,:amount,:amount,:currency,:paid_at,:action,:organic)"
                        ),
                        {
                            "merchant": demo["merchant_id"],
                            "mode": demo["provider_mode"],
                            "payment": attribution["payment_entity_id"],
                            "obligation": identity,
                            "amount": attribution["amount_paise"],
                            "currency": demo["currency"],
                            "paid_at": attribution["paid_at"],
                            "action": attribution["credited_action_id"],
                            "organic": attribution["organic"],
                        },
                    )
        # Preserve historical case/events/actions/attributions exactly. Ambiguous old owners
        # are quarantined by reconciliation_required; no silent rewriting or double credit.
        bind.execute(
            sa.text(
                "INSERT INTO provider_entities (merchant_id,session_id,provider,mode,entity_type,"
                "provider_entity_id,role,obligation_id,status,safe_metadata) VALUES "
                "(:merchant,:session,'razorpay',:mode,'order',:entity,'primary',:obligation,NULL,'{}')"
            ),
            {
                "merchant": demo["merchant_id"],
                "session": demo["id"],
                "mode": demo["provider_mode"],
                "entity": demo["primary_entity_id"],
                "obligation": identity,
            },
        )


def downgrade() -> None:
    raise RuntimeError("multi-resource foundation is forward-only; restore a reviewed backup")


def _create_tables() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "provider_obligations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("provider_entity_id", sa.String(), nullable=False),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("baseline_paid_paise", sa.BigInteger(), nullable=False),
        sa.Column("detected_due_paise", sa.BigInteger(), nullable=True),
        sa.Column("outstanding_paise", sa.BigInteger(), nullable=True),
        sa.Column("recovered_paise", sa.BigInteger(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciliation_required", sa.Boolean(), nullable=False),
        sa.Column("alias_of", sa.String(), nullable=True),
        sa.CheckConstraint("entity_type IN ('order', 'invoice')", name="ck_obligation_type"),
        sa.CheckConstraint("mode IN ('test', 'live')", name="ck_obligation_mode"),
        sa.CheckConstraint(
            "recovered_paise >= 0 AND baseline_paid_paise >= 0 AND "
            "(detected_due_paise IS NULL OR recovered_paise <= detected_due_paise)",
            name="ck_obligation_credit",
        ),
        sa.ForeignKeyConstraint(
            ["alias_of"],
            ["provider_obligations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["cases.id"],
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["case_id", "merchant_id"],
            ["cases.id", "cases.merchant_id"],
            name="fk_obligation_case_scope",
        ),
        sa.UniqueConstraint("case_id", name="uq_obligation_case"),
        sa.UniqueConstraint("id", "merchant_id", "provider", "mode", name="uq_obligation_scope"),
        sa.UniqueConstraint(
            "merchant_id",
            "provider",
            "mode",
            "entity_type",
            "provider_entity_id",
            name="uq_obligation_identity",
        ),
    )
    op.create_table(
        "provider_entities",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("provider_entity_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("root_entity_type", sa.String(), nullable=True),
        sa.Column("root_entity_id", sa.String(), nullable=True),
        sa.Column("obligation_id", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("state_observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "safe_metadata",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "entity_type IN ('order', 'invoice', 'subscription', 'payment', 'token')",
            name="ck_provider_entity_type",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
        ),
        sa.ForeignKeyConstraint(
            ["obligation_id", "merchant_id", "provider", "mode"],
            [
                "provider_obligations.id",
                "provider_obligations.merchant_id",
                "provider_obligations.provider",
                "provider_obligations.mode",
            ],
            name="fk_provider_entity_obligation",
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "merchant_id", "mode"],
            ["demo_sessions.id", "demo_sessions.merchant_id", "demo_sessions.provider_mode"],
            name="fk_provider_entity_session",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "merchant_id",
            "provider",
            "mode",
            "entity_type",
            "provider_entity_id",
            name="uq_provider_entity_identity",
        ),
    )
    op.create_index(
        "ix_provider_entities_session", "provider_entities", ["session_id"], unique=False
    )
    op.create_index(
        "ix_provider_entities_root",
        "provider_entities",
        ["merchant_id", "provider", "mode", "root_entity_type", "root_entity_id"],
        unique=False,
    )
    op.create_table(
        "provider_settlements",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("merchant_id", sa.String(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("payment_id", sa.String(), nullable=False),
        sa.Column("obligation_id", sa.String(), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("credited_paise", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("credited_action_id", sa.String(), nullable=True),
        sa.Column("organic", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "amount_paise >= 0 AND credited_paise >= 0 AND credited_paise <= amount_paise",
            name="ck_settlement_credit",
        ),
        sa.ForeignKeyConstraint(
            ["credited_action_id"],
            ["actions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["obligation_id", "merchant_id", "provider", "mode"],
            [
                "provider_obligations.id",
                "provider_obligations.merchant_id",
                "provider_obligations.provider",
                "provider_obligations.mode",
            ],
            name="fk_settlement_obligation",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "merchant_id", "provider", "mode", "payment_id", name="uq_provider_settlement_payment"
        ),
    )
    # ### end Alembic commands ###
