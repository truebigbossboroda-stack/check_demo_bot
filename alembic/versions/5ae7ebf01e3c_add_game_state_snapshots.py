"""add_game_state_snapshots

Revision ID: 5ae7ebf01e3c
Revises: 42e348f3d939
Create Date: 2026-02-02 11:05:48.649968

"""
from typing import Sequence, Union
from sqlalchemy.dialects import postgresql
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5ae7ebf01e3c'
down_revision: Union[str, Sequence[str], None] = '42e348f3d939'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "game_state_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("phase_seq", sa.Integer(), nullable=False),
        sa.Column("round_num", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["game_id"], ["game_sessions.id"], ondelete="CASCADE"),
        sa.CheckConstraint("phase_seq >= 0", name="ck_snapshots_phase_seq_ge_0"),
        sa.CheckConstraint("round_num >= 0", name="ck_snapshots_round_ge_0"),
    )

    op.create_index("ix_snapshots_game_created", "game_state_snapshots", ["game_id", "created_at"])
    op.create_index("ix_snapshots_chat_created", "game_state_snapshots", ["chat_id", "created_at"])


def downgrade():
    op.drop_index("ix_snapshots_chat_created", table_name="game_state_snapshots")
    op.drop_index("ix_snapshots_game_created", table_name="game_state_snapshots")
    op.drop_table("game_state_snapshots")
