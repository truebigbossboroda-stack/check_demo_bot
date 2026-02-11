"""init schema v1

Revision ID: fdecaa288dca
Revises: 
Create Date: 2026-01-21 13:11:48.904830

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'fdecaa288dca'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # UUID generator
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto;")

    # -------------------------
    # users
    # -------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("login", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("recovery_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("allow_multi_tg", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("login", name="uq_users_login"),
    )

    # -------------------------
    # user_tg_bindings
    # -------------------------
    op.create_table(
        "user_tg_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_username", sa.Text(), nullable=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("bound_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("tg_user_id", name="uq_user_tg_bindings_tg_user_id"),
    )
    op.create_index(
        "uq_user_tg_bindings_primary_per_user",
        "user_tg_bindings",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_primary = true AND revoked_at IS NULL"),
    )

    # -------------------------
    # auth_sessions
    # -------------------------
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_auth_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.CheckConstraint("expires_at > created_at", name="ck_auth_sessions_expires_after_created"),
    )
    op.create_index("ix_auth_sessions_user_tg_active", "auth_sessions", ["user_id", "tg_user_id", "is_revoked"])

    # -------------------------
    # admins
    # -------------------------
    op.create_table(
        "admins",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("user_id", name="uq_admins_user_id"),
        sa.CheckConstraint("role IN ('admin','superadmin')", name="ck_admins_role"),
    )

    # -------------------------
    # events / countries / cities
    # -------------------------
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("weight", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("weight >= 1", name="ck_events_weight_ge_1"),
    )

    op.create_table(
        "countries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("code", name="uq_countries_code"),
    )

    op.create_table(
        "cities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("country_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.UniqueConstraint("country_id", "name", name="uq_cities_country_name"),
    )

    # -------------------------
    # game_sessions
    # -------------------------
    op.create_table(
        "game_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'lobby'")),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=True),
        sa.Column("round_num", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("current_phase", sa.Text(), nullable=False, server_default=sa.text("'LOBBY'")),
        sa.Column("phase_seq", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("phase_started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("afk_timeout_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")),  # 5 minutes
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("archived_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("round_num >= 0", name="ck_game_sessions_round_ge_0"),
        sa.CheckConstraint("phase_seq >= 0", name="ck_game_sessions_phase_seq_ge_0"),
        sa.CheckConstraint("afk_timeout_seconds > 0", name="ck_game_sessions_afk_timeout_gt_0"),
        sa.CheckConstraint("status IN ('lobby','active','finished','archived')", name="ck_game_sessions_status"),
        sa.CheckConstraint("current_phase IN ('LOBBY','INCOME','ORDERS','RESOLVE','SUMMARY')", name="ck_game_sessions_phase"),
        sa.CheckConstraint("expires_at > created_at", name="ck_game_sessions_expires_after_created"),
    )
    op.create_index(
        "uq_game_sessions_one_active_per_chat",
        "game_sessions",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('lobby','active')"),
    )
    op.create_index("ix_game_sessions_chat_status", "game_sessions", ["chat_id", "status"])

    # -------------------------
    # game_players
    # -------------------------
    op.create_table(
        "game_players",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tg_user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("country_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("countries.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_afk", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_action_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("game_id", "country_id", name="uq_game_players_game_country"),
        sa.UniqueConstraint("game_id", "tg_user_id", name="uq_game_players_game_tg_user"),
    )
    op.create_index("ix_game_players_game_active", "game_players", ["game_id", "is_active", "is_afk"])

    # -------------------------
    # game_state_snapshots
    # -------------------------
    op.create_table(
        "game_state_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_num", sa.Integer(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("state_json", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("round_num >= 0", name="ck_snapshots_round_ge_0"),
    )
    op.create_index("ix_game_state_snapshots_latest", "game_state_snapshots", ["game_id", sa.text("created_at DESC")])

    # -------------------------
    # audit_log
    # -------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("details_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("actor_type IN ('admin','user','system')", name="ck_audit_actor_type"),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", [sa.text("created_at DESC")])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])

    # -------------------------
    # game_phase_ready (ready-check)
    # -------------------------
    op.create_table(
        "game_phase_ready",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("player_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("game_players.id", ondelete="CASCADE"), nullable=False),
        sa.Column("phase_seq", sa.Integer(), nullable=False),
        sa.Column("ready_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("phase_seq >= 0", name="ck_game_phase_ready_phase_seq_ge_0"),
        sa.UniqueConstraint("game_id", "player_id", "phase_seq", name="uq_game_phase_ready_once_per_phase"),
    )
    op.create_index("ix_game_phase_ready_game_phase", "game_phase_ready", ["game_id", "phase_seq"])

    # -------------------------
    # Triggers for ready-check correctness
    # -------------------------
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_ready_guard()
    RETURNS trigger AS $$
    DECLARE
        cur_phase_seq INT;
        p_game_id UUID;
        p_active BOOLEAN;
        p_afk BOOLEAN;
    BEGIN
        SELECT game_id, is_active, is_afk INTO p_game_id, p_active, p_afk
        FROM game_players
        WHERE id = NEW.player_id;

        IF p_game_id IS NULL THEN
            RAISE EXCEPTION 'Player not found';
        END IF;

        IF p_game_id <> NEW.game_id THEN
            RAISE EXCEPTION 'Player does not belong to this game';
        END IF;

        IF p_active IS NOT TRUE OR p_afk IS TRUE THEN
            RAISE EXCEPTION 'Player is not active or is AFK';
        END IF;

        SELECT phase_seq INTO cur_phase_seq
        FROM game_sessions
        WHERE id = NEW.game_id;

        IF cur_phase_seq IS NULL THEN
            RAISE EXCEPTION 'Game not found';
        END IF;

        IF NEW.phase_seq <> cur_phase_seq THEN
            RAISE EXCEPTION 'Ready is only allowed for current phase';
        END IF;

        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    DROP TRIGGER IF EXISTS ready_guard_before_insupd ON game_phase_ready;
    CREATE TRIGGER ready_guard_before_insupd
    BEFORE INSERT OR UPDATE ON game_phase_ready
    FOR EACH ROW EXECUTE FUNCTION trg_ready_guard();
    """)

    op.execute("""
    CREATE OR REPLACE FUNCTION trg_reset_ready_on_phase_change()
    RETURNS trigger AS $$
    BEGIN
        IF (NEW.phase_seq <> OLD.phase_seq) OR (NEW.current_phase <> OLD.current_phase) THEN
            DELETE FROM game_phase_ready
            WHERE game_id = NEW.id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    DROP TRIGGER IF EXISTS reset_ready_after_phase_change ON game_sessions;
    CREATE TRIGGER reset_ready_after_phase_change
    AFTER UPDATE OF phase_seq, current_phase ON game_sessions
    FOR EACH ROW EXECUTE FUNCTION trg_reset_ready_on_phase_change();
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS reset_ready_after_phase_change ON game_sessions;")
    op.execute("DROP FUNCTION IF EXISTS trg_reset_ready_on_phase_change();")
    op.execute("DROP TRIGGER IF EXISTS ready_guard_before_insupd ON game_phase_ready;")
    op.execute("DROP FUNCTION IF EXISTS trg_ready_guard();")

    op.drop_index("ix_game_phase_ready_game_phase", table_name="game_phase_ready")
    op.drop_table("game_phase_ready")

    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_game_state_snapshots_latest", table_name="game_state_snapshots")
    op.drop_table("game_state_snapshots")

    op.drop_index("ix_game_players_game_active", table_name="game_players")
    op.drop_table("game_players")

    op.drop_index("ix_game_sessions_chat_status", table_name="game_sessions")
    op.drop_index("uq_game_sessions_one_active_per_chat", table_name="game_sessions")
    op.drop_table("game_sessions")

    op.drop_table("cities")
    op.drop_table("countries")
    op.drop_table("events")

    op.drop_index("ix_auth_sessions_user_tg_active", table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index("uq_user_tg_bindings_primary_per_user", table_name="user_tg_bindings")
    op.drop_table("user_tg_bindings")

    op.drop_table("admins")
    op.drop_table("users")