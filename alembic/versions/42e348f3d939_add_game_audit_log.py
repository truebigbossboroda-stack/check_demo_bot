"""add_game_audit_log

Revision ID: 42e348f3d939
Revises: fe0300957582
Create Date: 2026-02-02 10:44:44.099552

"""
from typing import Sequence, Union
from sqlalchemy.dialects import postgresql
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42e348f3d939'
down_revision: Union[str, Sequence[str], None] = 'fe0300957582'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    op.create_table(
        "game_audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("phase_seq", sa.Integer(), nullable=True),
        sa.Column("round_num", sa.Integer(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["game_id"], ["game_sessions.id"], ondelete="CASCADE"),
    )

    op.create_index("ix_audit_game_time", "game_audit_log", ["game_id", "created_at"])
    op.create_index("ix_audit_chat_time", "game_audit_log", ["chat_id", "created_at"])
    op.create_index("ix_audit_action_time", "game_audit_log", ["action_type", "created_at"])

def downgrade():
    op.drop_index("ix_audit_action_time", table_name="game_audit_log")
    op.drop_index("ix_audit_chat_time", table_name="game_audit_log")
    op.drop_index("ix_audit_game_time", table_name="game_audit_log")
    op.drop_table("game_audit_log")