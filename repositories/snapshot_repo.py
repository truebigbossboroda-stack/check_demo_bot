import json
from sqlalchemy import text as sql_text

def insert_snapshot(
    db,
    *,
    game_id,
    chat_id: int,
    phase_seq: int,
    round_num: int,
    snapshot: dict,
):
    db.execute(
        sql_text("""
            INSERT INTO game_state_snapshots (game_id, chat_id, phase_seq, round_num, snapshot)
            VALUES (:game_id, :chat_id, :phase_seq, :round_num, CAST(:snapshot AS jsonb))
        """),
        {
            "game_id": str(game_id),
            "chat_id": chat_id,
            "phase_seq": phase_seq,
            "round_num": round_num,
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
        },
    )

def get_latest_snapshot_by_chat(db, chat_id: int):
    return db.execute(
        sql_text("""
            SELECT game_id, chat_id, phase_seq, round_num, snapshot, created_at
            FROM game_state_snapshots
            WHERE chat_id = :chat_id
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()