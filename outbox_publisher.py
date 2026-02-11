import os
import json
import time
import argparse
import socket
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text as sql_text

from kafka import KafkaProducer


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
TOPIC = os.getenv("KAFKA_TOPIC", "game-events")
DLQ_TOPIC = os.getenv("KAFKA_DLQ_TOPIC", "game-events.dlq")

BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "50"))
MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "10"))
POLL_SLEEP = float(os.getenv("OUTBOX_POLL_SLEEP_SEC", "0.5"))

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def _kafka_tcp_ping(bootstrap: str, timeout_sec: float = 1.0) -> bool:
    try:
        host, port_str = bootstrap.split(":")
        port = int(port_str)
    except Exception:
        return False

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout_sec)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _check_mode() -> int:
    # DB ping + outbox count
    with SessionLocal() as db:
        db.execute(sql_text("SELECT 1"))
        cnt = db.execute(sql_text("SELECT count(*) FROM outbox_events WHERE published_at IS NULL")).scalar_one()

    # Kafka ping
    kafka_ok = _kafka_tcp_ping(BOOTSTRAP)

    print(json.dumps({
        "ok": True,
        "db": "ok",
        "kafka": "ok" if kafka_ok else "fail",
        "kafka_bootstrap": BOOTSTRAP,
        "topic": TOPIC,
        "dlq_topic": DLQ_TOPIC,
        "outbox_unpublished": int(cnt),
        "time_utc": _utc_now_iso(),
    }, ensure_ascii=False))

    return 0 if kafka_ok else 2


def _json_default(o):
    # jsonb may contain timestamps/uuids serialized by psycopg already as python types in some setups
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(o)


def _mk_message(row: dict) -> dict:
    # row keys expected from outbox_events
    return {
        "schema_version": 1,
        "event_id": str(row["id"]),
        "type": row["event_type"],
        "aggregate": {
            "type": row["aggregate_type"],
            "id": str(row["aggregate_id"]),
        },
        "idempotency_key": row.get("idempotency_key"),
        "created_at": row["created_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if row.get("created_at")
        else _utc_now_iso(),
        "payload": row["payload"] or {},
    }


def _mark_published(db, event_id):
    db.execute(
        sql_text("""
            UPDATE outbox_events
            SET published_at = now(),
                publish_attempts = publish_attempts + 1,
                last_error = NULL
            WHERE id = :id
        """),
        {"id": event_id},
    )


def _mark_failed(db, event_id, err_text: str):
    db.execute(
        sql_text("""
            UPDATE outbox_events
            SET publish_attempts = publish_attempts + 1,
                last_error = :err
            WHERE id = :id
        """),
        {"id": event_id, "err": err_text[:4000]},
    )


def _mark_deadlettered(db, event_id, err_text: str):
    # Вариант A: не добавляем новые колонки — считаем DLQ "опубликовано"
    db.execute(
        sql_text("""
            UPDATE outbox_events
            SET published_at = now(),
                publish_attempts = publish_attempts + 1,
                last_error = :err
            WHERE id = :id
        """),
        {"id": event_id, "err": ("DLQ: " + err_text)[:4000]},
    )


def _fetch_batch_locked(db):
    # IMPORTANT: lock rows so parallel publishers don't duplicate work
    rows = db.execute(
        sql_text("""
            SELECT
                id,
                event_type,
                aggregate_type,
                aggregate_id,
                idempotency_key,
                payload,
                created_at,
                publish_attempts
            FROM outbox_events
            WHERE published_at IS NULL
              AND COALESCE(publish_attempts, 0) < :max_attempts
            ORDER BY created_at ASC
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        """),
        {"limit": BATCH_SIZE, "max_attempts": MAX_ATTEMPTS},
    ).mappings().all()
    return rows


def main():
    producer = KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: (k.encode("utf-8") if k else None),
        value_serializer=lambda v: json.dumps(v, default=_json_default, ensure_ascii=False).encode("utf-8"),
        acks="all",
        retries=3,
        linger_ms=10,
    )

    print(f"[publisher] bootstrap={BOOTSTRAP} topic={TOPIC} dlq={DLQ_TOPIC} batch={BATCH_SIZE}")

    while True:
        sent_any = False

        with SessionLocal() as db:
            # держим транзакцию только на чтение+лок, а апдейты делаем после publish тоже транзакционно
            with db.begin():
                batch = _fetch_batch_locked(db)

            if not batch:
                time.sleep(POLL_SLEEP)
                continue

            for row in batch:
                sent_any = True
                event_id = row["id"]
                attempts = int(row.get("publish_attempts") or 0)

                msg = _mk_message(row)
                key = str(row["aggregate_id"])

                if not msg.get("event_id") or not msg.get("type") or not msg.get("aggregate", {}).get("id"):
                    raise RuntimeError(f"bad message for outbox id={event_id}")

                try:
                    # send + sync wait (упрощённо и надёжно)
                    fut = producer.send(TOPIC, key=key, value=msg)
                    fut.get(timeout=10)

                    with db.begin():
                        _mark_published(db, event_id)

                except Exception as e:
                    err = f"{type(e).__name__}: {e}"

                    # если это последняя попытка — в DLQ и пометить как обработанное
                    if attempts + 1 >= MAX_ATTEMPTS:
                        dlq_msg = dict(msg)
                        dlq_msg["dlq"] = {
                            "failed_at": _utc_now_iso(),
                            "attempts": attempts + 1,
                            "error": err,
                        }
                        try:
                            fut2 = producer.send(DLQ_TOPIC, key=key, value=dlq_msg)
                            fut2.get(timeout=10)
                            with db.begin():
                                _mark_deadlettered(db, event_id, err)
                        except Exception as e2:
                            err2 = f"DLQ {type(e2).__name__}: {e2}"
                            with db.begin():
                                _mark_failed(db, event_id, err2)
                    else:
                        with db.begin():
                            _mark_failed(db, event_id, err)

        if not sent_any:
            time.sleep(POLL_SLEEP)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="readiness check (db + kafka tcp) and exit")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(_check_mode())

    main()