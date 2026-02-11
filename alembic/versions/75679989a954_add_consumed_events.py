"""add_consumed_events

Revision ID: 75679989a954
Revises: 0d08784e0159
Create Date: 2026-02-03 13:11:18.349097

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "75679989a954"
down_revision = "0d08784e0159"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "consumed_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("partition", sa.Integer(), nullable=False),
        sa.Column("offset", sa.BigInteger(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=True),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_unique_constraint("uq_consumed_events_event_id", "consumed_events", ["event_id"])
    op.create_index("ix_consumed_events_aggregate", "consumed_events", ["aggregate_type", "aggregate_id"])

def downgrade():
    op.drop_index("ix_consumed_events_aggregate", table_name="consumed_events")
    op.drop_constraint("uq_consumed_events_event_id", "consumed_events", type_="unique")
    op.drop_table("consumed_events")