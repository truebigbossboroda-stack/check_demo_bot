import json
from sqlalchemy import text as sql_text

def audit_log(
    db,
    *,
    game_id,
    chat_id: int,
    action_type: str,
    actor_tg_user_id: int | None,
    phase_seq: int | None,
    round_num: int | None,
    payload: dict | None = None,
) -> None:
    if payload is None:
        payload = {}

    db.execute(
        sql_text("""
            INSERT INTO game_audit_log
                (game_id, chat_id, actor_tg_user_id, action_type, phase_seq, round_num, payload)
            VALUES
                (:game_id, :chat_id, :actor, :action, :phase_seq, :round_num, CAST(:payload AS jsonb))
        """),
        {
            "game_id": str(game_id),
            "chat_id": chat_id,
            "actor": actor_tg_user_id,
            "action": action_type,
            "phase_seq": phase_seq,
            "round_num": round_num,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )