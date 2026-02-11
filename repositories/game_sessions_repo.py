from sqlalchemy import text as sql_text

def get_current_session(db, chat_id: int):
    return db.execute(
        sql_text("""
            SELECT *
            FROM game_sessions
            WHERE chat_id = :chat_id
              AND status IN ('lobby','active')
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()

def get_current_session_id(db, chat_id: int):
    row = db.execute(
        sql_text("""
            SELECT id
            FROM game_sessions
            WHERE chat_id = :chat_id
              AND status IN ('lobby','active')
            ORDER BY created_at DESC
            LIMIT 1
        """),
        {"chat_id": chat_id},
    ).mappings().first()
    return row["id"] if row else None