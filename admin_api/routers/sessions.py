from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/sessions", tags=["sessions"])

@router.get("/current")
def current_session(
    chat_id: int = Query(...),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    row = db.execute(
        sql_text("""
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
            FROM v_current_game_by_chat rm
            WHERE rm.chat_id = :chat_id
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()

    return {"chat_id": chat_id, "current": row}

@router.get("/history")
def session_history(
    chat_id: int = Query(...),
    limit: int = Query(50, ge=1, le=200),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        sql_text("""
            SELECT
                id, chat_id, status, owner_tg_user_id,
                round_num, current_phase, phase_seq,
                phase_started_at, created_at, expires_at, archived_at
            FROM game_sessions
            WHERE chat_id = :chat_id
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"chat_id": chat_id, "limit": limit},
    ).mappings().all()

    return {"chat_id": chat_id, "items": rows, "limit": limit}