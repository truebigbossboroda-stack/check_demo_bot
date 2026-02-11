from fastapi import APIRouter, Depends, Query
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

from ..auth import require_bearer
from ..db import get_db

router = APIRouter(prefix="/players", tags=["players"])

@router.get("/by-chat")
def players_by_chat(
    chat_id: int = Query(...),
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
                id, game_id, tg_user_id, user_id, country_id,
                joined_at, is_active, is_afk, last_action_at
            FROM game_players
            WHERE game_id = :game_id
            ORDER BY joined_at ASC
        """),
        {"game_id": game_id},
    ).mappings().all()

    return {"chat_id": chat_id, "game_id": game_id, "items": rows}

@router.get("/by-game")
def players_by_game(
    game_id: str = Query(...),
    _: bool = Depends(require_bearer),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        sql_text("""
            SELECT
                id, game_id, tg_user_id, user_id, country_id,
                joined_at, is_active, is_afk, last_action_at
            FROM game_players
            WHERE game_id = :game_id
            ORDER BY joined_at ASC
        """),
        {"game_id": game_id},
    ).mappings().all()
    return {"game_id": game_id, "items": rows}