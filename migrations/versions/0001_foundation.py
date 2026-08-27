"""Create the Leakproof foundation schema.

Revision ID: 0001_foundation
Revises:
Create Date: 2026-08-25
"""

from alembic import op

from leakproof.db import Base
from leakproof.models import db  # noqa: F401

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The first migration intentionally mirrors the reviewed ORM metadata. Future
    # migrations must use explicit Alembic operations so this baseline stays frozen.
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
