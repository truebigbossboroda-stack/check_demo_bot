from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/outbox", tags=["outbox"])

@router.get("/by-chat")
def outbox_by_chat(
    chat_id: int = Query(...),
    limit: int = Query(200, ge=1, le=500),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rm = db.execute(
        sql_text("SELECT game_id FROM v_current_game_by_chat WHERE chat_id=:chat_id"),
        {"chat_id": chat_id},
    ).mappings().first()

    if not rm:
        return {"chat_id": chat_id, "game_id": None, "items": []}

    game_id = rm["game_id"]

    rows = db.execute(
        sql_text("""
            SELECT
                id, event_type, aggregate_type, aggregate_id, idempotency_key,
                created_at, published_at, publish_attempts, last_error, payload
            FROM outbox_events
            WHERE aggregate_type = 'game_session'
              AND aggregate_id = :game_id
            ORDER BY created_at DESC
            LIMIT :limit
        """),
        {"game_id": game_id, "limit": limit},
    ).mappings().all()

    return {"chat_id": chat_id, "game_id": game_id, "items": rows, "limit": limit}

@router.get("/unpublished")
def outbox_unpublished(
    limit: int = Query(200, ge=1, le=1000),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        sql_text("""
            SELECT
                id, event_type, aggregate_type, aggregate_id, idempotency_key,
                created_at, publish_attempts, last_error, payload
            FROM outbox_events
            WHERE published_at IS NULL
            ORDER BY created_at ASC
            LIMIT :limit
        """),
        {"limit": limit},
    ).mappings().all()

    return {"items": rows, "limit": limit}