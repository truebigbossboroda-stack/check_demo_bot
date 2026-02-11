"""add outbox_events

Revision ID: b122cb68d723
Revises: 391df6c976ae
Create Date: 2026-01-29 14:52:34.542492

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "b122cb68d723"
down_revision = "391df6c976ae"
branch_labels = None
depends_on = None


def upgrade():
    # Required for gen_random_uuid() defaults used in this and later migrations.
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "outbox_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "publish_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
    )

    # NOTE:
    # idempotency_key is added in the next migration (fe0300957582) to keep the history consistent
    # and avoid duplicate column/index creation on a fresh database.

    op.create_index(
        "ix_outbox_events_unpublished",
        "outbox_events",
        ["published_at", "created_at"],
    )

    op.create_index(
        "ix_outbox_events_agg",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_outbox_events_agg", table_name="outbox_events")
    op.drop_index("ix_outbox_events_unpublished", table_name="outbox_events")
    op.drop_table("outbox_events")
