"""Enforce immutable case events at the PostgreSQL database boundary.

Revision ID: 0002_append_only_events
Revises: 0001_foundation
Create Date: 2026-08-25
"""

from alembic import op

revision = "0002_append_only_events"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION reject_case_event_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION 'case events are append-only: % is forbidden', TG_OP;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER enforce_append_only_case_events
        BEFORE UPDATE OR DELETE ON events
        FOR EACH ROW
        EXECUTE FUNCTION reject_case_event_mutation()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER enforce_append_only_case_events ON events")
    op.execute("DROP FUNCTION reject_case_event_mutation()")
