from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/audit", tags=["audit"])

@router.get("/by-chat")
def audit_by_chat(
    chat_id: int = Query(...),
    limit: int = Query(100, ge=1, le=500),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        sql_text("""
            SELECT
                id, game_id, chat_id, actor_tg_user_id, action_type,
                phase_seq, round_num, payload, created_at
            FROM game_audit_log
            WHERE chat_id = :chat_id
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"chat_id": chat_id, "limit": limit},
    ).mappings().all()

    return {"chat_id": chat_id, "items": rows, "limit": limit}