"""add_outbox_idempotency_key

Revision ID: fe0300957582
Revises: b122cb68d723
Create Date: 2026-01-31 21:00:01.724660

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fe0300957582'
down_revision: Union[str, Sequence[str], None] = 'b122cb68d723'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.add_column("outbox_events", sa.Column("idempotency_key", sa.Text(), nullable=True))
    op.create_index(
        "uq_outbox_events_idem_key",
        "outbox_events",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade():
    op.drop_index("uq_outbox_events_idem_key", table_name="outbox_events")
    op.drop_column("outbox_events", "idempotency_key")