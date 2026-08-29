"""Enforce one recovery email outcome per case.

Revision ID: 0009_resend_delivery_guard
Revises: 0008_live_demo_contracts
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0009_resend_delivery_guard"
down_revision = "0008_live_demo_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if "email_deliveries" not in set(sa.inspect(bind).get_table_names()):
        return
    constraints = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("email_deliveries")
    }
    if "uq_email_deliveries_case" not in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("email_deliveries") as batch:
                batch.create_unique_constraint("uq_email_deliveries_case", ["case_id"])
        else:
            op.create_unique_constraint(
                "uq_email_deliveries_case", "email_deliveries", ["case_id"]
            )


def downgrade() -> None:
    bind = op.get_bind()
    if "email_deliveries" not in set(sa.inspect(bind).get_table_names()):
        return
    constraints = {
        item["name"] for item in sa.inspect(bind).get_unique_constraints("email_deliveries")
    }
    if "uq_email_deliveries_case" in constraints:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("email_deliveries") as batch:
                batch.drop_constraint("uq_email_deliveries_case", type_="unique")
        else:
            op.drop_constraint(
                "uq_email_deliveries_case", "email_deliveries", type_="unique"
            )
