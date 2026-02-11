from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/ready", tags=["ready"])

@router.get("/summary")
def ready_summary(
    chat_id: int = Query(...),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    row = db.execute(
        sql_text("""
            SELECT chat_id, game_id, phase_seq, ready_count, ready_total, updated_at
            FROM v_current_game_by_chat
            WHERE chat_id = :chat_id
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()
    return {"chat_id": chat_id, "summary": row}

@router.get("/current")
def ready_current(
    chat_id: int = Query(...),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rm = db.execute(
        sql_text("""
            SELECT game_id, phase_seq
            FROM v_current_game_by_chat
            WHERE chat_id=:chat_id
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()

    if not rm:
        return {"chat_id": chat_id, "game_id": None, "phase_seq": None, "items": []}

    game_id = rm["game_id"]
    phase_seq = rm["phase_seq"]

    rows = db.execute(
        sql_text("""
            SELECT
                r.id AS ready_id,
                r.ready_at,
                p.id AS player_id,
                p.tg_user_id,
                p.country_id
            FROM game_phase_ready r
            JOIN game_players p ON p.id = r.player_id
            WHERE r.game_id = :game_id
              AND r.phase_seq = :phase_seq
              AND p.is_active = true
              AND p.is_afk = false
            ORDER BY r.ready_at ASC
        """),
        {"game_id": game_id, "phase_seq": phase_seq},
    ).mappings().all()

    return {"chat_id": chat_id, "game_id": game_id, "phase_seq": phase_seq, "items": rows}