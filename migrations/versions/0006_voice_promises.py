"""Add bounded voice-turn persistence and promise idempotency.

Revision ID: 0006_voice_promises
Revises: 0005_evaluations
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_voice_promises"
down_revision = "0005_evaluations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "voice_turns" not in tables:
        op.create_table(
            "voice_turns",
            sa.Column("provider_turn_id", sa.String(), primary_key=True),
            sa.Column("action_id", sa.String(), sa.ForeignKey("actions.id"), nullable=False),
            sa.Column("case_id", sa.String(), sa.ForeignKey("cases.id"), nullable=False),
            sa.Column("turn_number", sa.SmallInteger(), nullable=False),
            sa.Column("transcript", sa.Text(), nullable=False),
            sa.Column("intent", sa.String(), nullable=False),
            sa.Column("reply_template_id", sa.String(), nullable=False),
            sa.Column("ended", sa.Boolean(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "action_id", "turn_number", name="uq_voice_turn_action_number"
            ),
        )

    promise_constraints = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("promises")
    }
    if "uq_promises_case_transcript" not in promise_constraints:
        op.create_unique_constraint(
            "uq_promises_case_transcript",
            "promises",
            ["case_id", "transcript_ref"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "voice_turns" in inspector.get_table_names():
        op.drop_table("voice_turns")
    promise_constraints = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("promises")
    }
    if "uq_promises_case_transcript" in promise_constraints:
        op.drop_constraint(
            "uq_promises_case_transcript", "promises", type_="unique"
        )
