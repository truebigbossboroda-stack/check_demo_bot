from sqlalchemy import text
from sqlalchemy.orm import Session


def mark_ready(session: Session, game_id: str, player_id: str, phase_seq: int):
    # Триггер сам проверит, что phase_seq = текущей фазе
    q = text("""
        INSERT INTO game_phase_ready (game_id, player_id, phase_seq)
        VALUES (:game_id, :player_id, :phase_seq)
        ON CONFLICT (game_id, player_id, phase_seq) DO NOTHING
    """)
    session.execute(q, {"game_id": game_id, "player_id": player_id, "phase_seq": phase_seq})


def count_ready(session: Session, game_id: str, phase_seq: int) -> int:
    q = text("""
        SELECT count(*)::int AS cnt
        FROM game_phase_ready
        WHERE game_id = :game_id
          AND phase_seq = :phase_seq
    """)
    return session.execute(q, {"game_id": game_id, "phase_seq": phase_seq}).scalar_one()