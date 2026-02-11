"""add_read_model_and_views

Revision ID: 0d08784e0159
Revises: 5ae7ebf01e3c
Create Date: 2026-02-02 12:49:44.550179

"""
from typing import Sequence, Union
from alembic import op
from sqlalchemy.dialects import postgresql
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0d08784e0159'
down_revision: Union[str, Sequence[str], None] = '5ae7ebf01e3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade():
    # 1) table: game_read_model
    op.create_table(
        "game_read_model",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True),
        sa.Column("game_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("current_phase", sa.Text(), nullable=False),
        sa.Column("phase_seq", sa.Integer(), nullable=False),
        sa.Column("round_num", sa.Integer(), nullable=False),
        sa.Column("phase_started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("owner_tg_user_id", sa.BigInteger(), nullable=True),

        sa.Column("players_total", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("players_active", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("ready_total", sa.Integer(), nullable=False, server_default=sa.text("0")),

        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["game_id"], ["game_sessions.id"], ondelete="CASCADE"),
    )

    op.create_index("ix_game_read_model_game_id", "game_read_model", ["game_id"], unique=True)

    # 2) helper function: recompute row by game_id
    op.execute("""
    CREATE OR REPLACE FUNCTION recompute_game_read_model(p_game_id uuid)
    RETURNS void AS $$
    DECLARE
        v_chat_id bigint;
        v_status text;
        v_phase text;
        v_phase_seq int;
        v_round int;
        v_phase_started_at timestamptz;
        v_expires_at timestamptz;
        v_owner bigint;
        v_players_total int;
        v_players_active int;
        v_ready_count int;
        v_ready_total int;
    BEGIN
        SELECT chat_id, status, current_phase, phase_seq, round_num, phase_started_at, expires_at, owner_tg_user_id
        INTO v_chat_id, v_status, v_phase, v_phase_seq, v_round, v_phase_started_at, v_expires_at, v_owner
        FROM game_sessions
        WHERE id = p_game_id;

        IF v_chat_id IS NULL THEN
            -- игра удалена/не найдена => чистим read_model строку, если была
            DELETE FROM game_read_model WHERE game_id = p_game_id;
            RETURN;
        END IF;

        SELECT count(*)::int
        INTO v_players_total
        FROM game_players
        WHERE game_id = p_game_id;

        SELECT count(*)::int
        INTO v_players_active
        FROM game_players
        WHERE game_id = p_game_id
          AND is_active = true
          AND is_afk = false;

        SELECT count(*)::int
        INTO v_ready_count
        FROM game_phase_ready r
        JOIN game_players p ON p.id = r.player_id
        WHERE r.game_id = p_game_id
          AND r.phase_seq = v_phase_seq
          AND p.is_active = true
          AND p.is_afk = false;

        v_ready_total := v_players_active;

        INSERT INTO game_read_model (
            chat_id, game_id, status, current_phase, phase_seq, round_num,
            phase_started_at, expires_at, owner_tg_user_id,
            players_total, players_active, ready_count, ready_total, updated_at
        )
        VALUES (
            v_chat_id, p_game_id, v_status, v_phase, v_phase_seq, v_round,
            v_phase_started_at, v_expires_at, v_owner,
            COALESCE(v_players_total,0), COALESCE(v_players_active,0),
            COALESCE(v_ready_count,0), COALESCE(v_ready_total,0),
            now()
        )
        ON CONFLICT (chat_id) DO UPDATE SET
            game_id = EXCLUDED.game_id,
            status = EXCLUDED.status,
            current_phase = EXCLUDED.current_phase,
            phase_seq = EXCLUDED.phase_seq,
            round_num = EXCLUDED.round_num,
            phase_started_at = EXCLUDED.phase_started_at,
            expires_at = EXCLUDED.expires_at,
            owner_tg_user_id = EXCLUDED.owner_tg_user_id,
            players_total = EXCLUDED.players_total,
            players_active = EXCLUDED.players_active,
            ready_count = EXCLUDED.ready_count,
            ready_total = EXCLUDED.ready_total,
            updated_at = EXCLUDED.updated_at;

    END;
    $$ LANGUAGE plpgsql;
    """)

    # 3) triggers to keep read_model in sync (sync-materialization until Kafka in Block E)

    # 3.1) game_sessions => recompute by NEW.id
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_rm_on_game_sessions()
    RETURNS trigger AS $$
    BEGIN
        PERFORM recompute_game_read_model(NEW.id);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    DROP TRIGGER IF EXISTS rm_on_game_sessions_aiu ON game_sessions;
    CREATE TRIGGER rm_on_game_sessions_aiu
    AFTER INSERT OR UPDATE ON game_sessions
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_sessions();
    """)

    # 3.2) game_players => recompute by NEW.game_id (or OLD.game_id on delete)
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_rm_on_game_players()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            PERFORM recompute_game_read_model(OLD.game_id);
            RETURN OLD;
        ELSE
            PERFORM recompute_game_read_model(NEW.game_id);
            RETURN NEW;
        END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    DROP TRIGGER IF EXISTS rm_on_game_players_aiud ON game_players;
    CREATE TRIGGER rm_on_game_players_aiud
    AFTER INSERT OR UPDATE OR DELETE ON game_players
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_players();
    """)

    # 3.3) game_phase_ready => recompute by NEW.game_id (or OLD.game_id on delete)
    op.execute("""
    CREATE OR REPLACE FUNCTION trg_rm_on_game_phase_ready()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            PERFORM recompute_game_read_model(OLD.game_id);
            RETURN OLD;
        ELSE
            PERFORM recompute_game_read_model(NEW.game_id);
            RETURN NEW;
        END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)

    op.execute("""
    DROP TRIGGER IF EXISTS rm_on_game_phase_ready_aiud ON game_phase_ready;
    CREATE TRIGGER rm_on_game_phase_ready_aiud
    AFTER INSERT OR UPDATE OR DELETE ON game_phase_ready
    FOR EACH ROW EXECUTE FUNCTION trg_rm_on_game_phase_ready();
    """)

    # 4) views

    # v_current_game_by_chat: быстрый "текущая игра в чате"
    op.execute("""
    CREATE OR REPLACE VIEW v_current_game_by_chat AS
    SELECT
        rm.chat_id,
        rm.game_id,
        rm.status,
        rm.current_phase,
        rm.phase_seq,
        rm.round_num,
        rm.phase_started_at,
        rm.expires_at,
        rm.owner_tg_user_id,
        rm.players_total,
        rm.players_active,
        rm.ready_count,
        rm.ready_total,
        rm.updated_at
    FROM game_read_model rm;
    """)

    # v_ready_status_current_phase: отдельно (если хочешь без read_model, но оставим как витрину)
    op.execute("""
    CREATE OR REPLACE VIEW v_ready_status_current_phase AS
    SELECT
        gs.chat_id,
        gs.id AS game_id,
        gs.phase_seq,
        count(r.id)::int AS ready_count,
        (
          SELECT count(*)::int
          FROM game_players p
          WHERE p.game_id = gs.id
            AND p.is_active = true
            AND p.is_afk = false
        ) AS total_active
    FROM game_sessions gs
    LEFT JOIN game_phase_ready r
      ON r.game_id = gs.id AND r.phase_seq = gs.phase_seq
    LEFT JOIN game_players p
      ON p.id = r.player_id
     AND p.is_active = true
     AND p.is_afk = false
    WHERE gs.status IN ('lobby','active')
    GROUP BY gs.chat_id, gs.id, gs.phase_seq;
    """)

    # 5) backfill read_model for existing active/lobby games
    op.execute("""
    DO $$
    DECLARE r record;
    BEGIN
        FOR r IN
            SELECT id FROM game_sessions WHERE status IN ('lobby','active')
        LOOP
            PERFORM recompute_game_read_model(r.id);
        END LOOP;
    END;
    $$;
    """)


def downgrade():
    op.execute("DROP VIEW IF EXISTS v_ready_status_current_phase;")
    op.execute("DROP VIEW IF EXISTS v_current_game_by_chat;")

    op.execute("DROP TRIGGER IF EXISTS rm_on_game_phase_ready_aiud ON game_phase_ready;")
    op.execute("DROP FUNCTION IF EXISTS trg_rm_on_game_phase_ready();")

    op.execute("DROP TRIGGER IF EXISTS rm_on_game_players_aiud ON game_players;")
    op.execute("DROP FUNCTION IF EXISTS trg_rm_on_game_players();")

    op.execute("DROP TRIGGER IF EXISTS rm_on_game_sessions_aiu ON game_sessions;")
    op.execute("DROP FUNCTION IF EXISTS trg_rm_on_game_sessions();")

    op.execute("DROP FUNCTION IF EXISTS recompute_game_read_model(uuid);")

    op.drop_index("ix_game_read_model_game_id", table_name="game_read_model")
    op.drop_table("game_read_model")