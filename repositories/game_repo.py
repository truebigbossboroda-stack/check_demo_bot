from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session


def get_active_game_by_chat(session: Session, chat_id: int):
    q = sql_text("""
        SELECT
            rm.game_id AS id,
            rm.chat_id,
            rm.status,
            rm.phase_seq,
            rm.current_phase,
            gs.afk_timeout_seconds,
            gs.round_num,
            rm.players_total,
            rm.players_active,
            rm.ready_count,
            rm.ready_total,
            rm.updated_at
        FROM v_current_game_by_chat rm
        JOIN game_sessions gs ON gs.id = rm.game_id
        WHERE rm.chat_id = :chat_id
        LIMIT 1
    """)
    return session.execute(q, {"chat_id": chat_id}).mappings().first()


def get_player_in_game(session: Session, game_id: str, tg_user_id: int):
    q = sql_text("""
        SELECT id, game_id, tg_user_id, is_active, is_afk
        FROM game_players
        WHERE game_id = :game_id
          AND tg_user_id = :tg_user_id
        LIMIT 1
    """)
    return session.execute(q, {"game_id": game_id, "tg_user_id": tg_user_id}).mappings().first()


def lock_game_row(session, chat_id: int):
    q = sql_text("""
        SELECT id, chat_id, status, current_phase, phase_seq, round_num
        FROM game_sessions
        WHERE chat_id = :chat_id
          AND status IN ('lobby','active')
        ORDER BY created_at DESC
        LIMIT 1
        FOR UPDATE
    """)
    return session.execute(q, {"chat_id": chat_id}).mappings().first()


def set_phase(session: Session, game_id: str, new_phase_seq: int, new_phase: str):
    q = sql_text("""
        UPDATE game_sessions
        SET phase_seq = :phase_seq,
            current_phase = :phase,
            phase_started_at = now()
        WHERE id = :game_id
    """)
    session.execute(q, {"phase_seq": new_phase_seq, "phase": new_phase, "game_id": game_id})


def count_active_players(session: Session, game_id: str) -> int:
    q = sql_text("""
        SELECT count(*)::int AS cnt
        FROM game_players
        WHERE game_id = :game_id
          AND is_active IS TRUE
          AND is_afk IS FALSE
    """)
    return session.execute(q, {"game_id": game_id}).scalar_one()