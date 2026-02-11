import json
from sqlalchemy import text as sql_text
from db import SessionLocal

BATCH_SIZE = 50

def main():
    with SessionLocal() as db:
        with db.begin():
            rows = db.execute(sql_text("""
                SELECT id, event_type, aggregate_type, aggregate_id, payload, created_at
                FROM outbox_events
                WHERE published_at IS NULL
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT :n
            """), {"n": BATCH_SIZE}).mappings().all()

        # ВАЖНО: публикацию делаем ВНЕ транзакции захвата, чтобы не держать lock долго
        for r in rows:
            try:
                payload = r["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)

                # TODO позже: отправка в Kafka
                print("PUBLISH", r["event_type"], r["aggregate_id"], payload)

                # отметить опубликованным
                with db.begin():
                    db.execute(sql_text("""
                        UPDATE outbox_events
                        SET published_at = now()
                        WHERE id = :id
                    """), {"id": str(r["id"])})

            except Exception as e:
                with db.begin():
                    db.execute(sql_text("""
                        UPDATE outbox_events
                        SET publish_attempts = publish_attempts + 1,
                            last_error = :err
                        WHERE id = :id
                    """), {"id": str(r["id"]), "err": str(e)})

if __name__ == "__main__":
    main()