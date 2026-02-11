"""add outbox state machine

Revision ID: bf963ac052e2
Revises: da80e3161c47
Create Date: 2026-02-11 14:15:01.401631

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'bf963ac052e2'
down_revision: Union[str, Sequence[str], None] = 'da80e3161c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("outbox_events", sa.Column("status", sa.Text(), nullable=False, server_default="new"))
    op.add_column("outbox_events", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("lock_owner", sa.Text(), nullable=True))
    op.add_column("outbox_events", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))

    # Индекс для выборки “готовых” событий
    op.create_index(
        "ix_outbox_events_ready",
        "outbox_events",
        ["status", "next_retry_at", "created_at"],
        unique=False,
    )


def downgrade():
    op.drop_index("ix_outbox_events_ready", table_name="outbox_events")
    op.drop_column("outbox_events", "next_retry_at")
    op.drop_column("outbox_events", "lock_owner")
    op.drop_column("outbox_events", "locked_until")
    op.drop_column("outbox_events", "status")
