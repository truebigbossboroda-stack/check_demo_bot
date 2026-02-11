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

# ----------------------------
# Config
# ----------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
TOPIC = os.getenv("KAFKA_TOPIC", "game-events")
DLQ_TOPIC = os.getenv("KAFKA_DLQ_TOPIC", "game-events.dlq")

BATCH_SIZE = int(os.getenv("OUTBOX_BATCH_SIZE", "50"))
MAX_ATTEMPTS = int(os.getenv("OUTBOX_MAX_ATTEMPTS", "10"))  # attempts before switching to DLQ path
IDLE_SLEEP = float(os.getenv("OUTBOX_POLL_SLEEP_SEC", "0.5"))

LOCK_TTL_SEC = int(os.getenv("OUTBOX_LOCK_TTL_SEC", "30"))
PUBLISH_TIMEOUT_SEC = float(os.getenv("OUTBOX_PUBLISH_TIMEOUT_SEC", "10"))

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

OWNER = f"{socket.gethostname()}:{os.getpid()}"


# ----------------------------
# Helpers
# ----------------------------
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
    # DB ping + outbox count (new+processing and unpublished)
    with SessionLocal() as db:
        db.execute(sql_text("SELECT 1"))
        cnt = db.execute(sql_text("""
            SELECT count(*)
            FROM outbox_events
            WHERE published_at IS NULL
              AND status IN ('new','processing')
        """)).scalar_one()

    kafka_ok = _kafka_tcp_ping(BOOTSTRAP)

    print(json.dumps({
        "ok": True,
        "db": "ok",
        "kafka": "ok" if kafka_ok else "fail",
        "kafka_bootstrap": BOOTSTRAP,
        "topic": TOPIC,
        "dlq_topic": DLQ_TOPIC,
        "outbox_pending": int(cnt),
        "owner": OWNER,
        "time_utc": _utc_now_iso(),
    }, ensure_ascii=False))

    return 0 if kafka_ok else 2


def _json_default(o):
    if isinstance(o, datetime):
        return o.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(o)


def backoff_seconds(attempt: int, base: int = 2, cap: int = 60) -> int:
    """
    attempt starts from 1
    2,4,8,16,32,60,60...
    """
    if attempt < 1:
        attempt = 1
    delay = base ** min(attempt, 6)
    return min(delay, cap)


def _mk_message(row: dict) -> dict:
    # event envelope (keep it stable; it is your contract)
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


# ----------------------------
# SQL (reserve / finalize / reclaim)
# ----------------------------
RECLAIM_SQL = """
UPDATE outbox_events
SET status='new',
    locked_until=NULL,
    lock_owner=NULL
WHERE status='processing'
  AND locked_until IS NOT NULL
  AND locked_until < now();
"""

RESERVE_SQL = """
WITH picked AS (
  SELECT id
  FROM outbox_events
  WHERE published_at IS NULL
    AND status = 'new'
    AND (next_retry_at IS NULL OR next_retry_at <= now())
  ORDER BY created_at ASC
  FOR UPDATE SKIP LOCKED
  LIMIT :limit
)
UPDATE outbox_events o
SET status = 'processing',
    locked_until = now() + (:lock_ttl_sec || ' seconds')::interval,
    lock_owner = :owner
FROM picked
WHERE o.id = picked.id
RETURNING
  o.id,
  o.event_type,
  o.aggregate_type,
  o.aggregate_id,
  o.idempotency_key,
  o.payload,
  o.created_at,
  o.publish_attempts,
  o.last_error;
"""

MARK_SENT_SQL = """
UPDATE outbox_events
SET status='sent',
    published_at=now(),
    last_error=NULL,
    locked_until=NULL,
    lock_owner=NULL,
    next_retry_at=NULL
WHERE id=:id
  AND status='processing'
  AND lock_owner=:owner;
"""

MARK_RETRY_SQL = """
UPDATE outbox_events
SET status='new',
    publish_attempts=publish_attempts+1,
    last_error=:err,
    next_retry_at=now() + (:delay_sec || ' seconds')::interval,
    locked_until=NULL,
    lock_owner=NULL
WHERE id=:id
  AND status='processing'
  AND lock_owner=:owner;
"""

MARK_DEAD_SQL = """
UPDATE outbox_events
SET status='dead',
    published_at=now(),
    publish_attempts=publish_attempts+1,
    last_error=:err,
    locked_until=NULL,
    lock_owner=NULL,
    next_retry_at=NULL
WHERE id=:id
  AND status='processing'
  AND lock_owner=:owner;
"""


def reclaim_stuck_processing(db) -> int:
    res = db.execute(sql_text(RECLAIM_SQL))
    return getattr(res, "rowcount", 0) or 0


def reserve_batch(db, *, limit: int, lock_ttl_sec: int, owner: str):
    rows = db.execute(
        sql_text(RESERVE_SQL),
        {"limit": limit, "lock_ttl_sec": lock_ttl_sec, "owner": owner},
    ).mappings().all()
    return rows


def mark_sent(db, *, event_id: str, owner: str) -> None:
    db.execute(sql_text(MARK_SENT_SQL), {"id": event_id, "owner": owner})


def mark_retry(db, *, event_id: str, owner: str, err: str, delay_sec: int) -> None:
    db.execute(
        sql_text(MARK_RETRY_SQL),
        {"id": event_id, "owner": owner, "err": err[:4000], "delay_sec": delay_sec},
    )


def mark_dead(db, *, event_id: str, owner: str, err: str) -> None:
    db.execute(
        sql_text(MARK_DEAD_SQL),
        {"id": event_id, "owner": owner, "err": err[:4000]},
    )


# ----------------------------
# Publish
# ----------------------------
def build_producer() -> KafkaProducer:
    return KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        key_serializer=lambda k: (k.encode("utf-8") if k else None),
        value_serializer=lambda v: json.dumps(v, default=_json_default, ensure_ascii=False).encode("utf-8"),
        acks="all",
        retries=3,
        linger_ms=10,
    )


def send_sync(producer: KafkaProducer, topic: str, key: str, value: dict, timeout_sec: float):
    fut = producer.send(topic, key=key, value=value)
    fut.get(timeout=timeout_sec)


# ----------------------------
# Main loop
# ----------------------------
def main():
    producer = build_producer()
    print(f"[relay] owner={OWNER} bootstrap={BOOTSTRAP} topic={TOPIC} dlq={DLQ_TOPIC} "
          f"batch={BATCH_SIZE} max_attempts={MAX_ATTEMPTS} lock_ttl={LOCK_TTL_SEC}s")

    try:
        while True:
            # 1) reclaim expired processing locks
            with SessionLocal() as db:
                with db.begin():
                    reclaimed = reclaim_stuck_processing(db)
            if reclaimed:
                print(f"[relay] reclaimed={reclaimed}")

            # 2) reserve batch (transaction)
            with SessionLocal() as db:
                with db.begin():
                    batch = reserve_batch(db, limit=BATCH_SIZE, lock_ttl_sec=LOCK_TTL_SEC, owner=OWNER)

            if not batch:
                time.sleep(IDLE_SLEEP)
                continue

            # 3) publish each event + finalize
            for row in batch:
                event_id = str(row["id"])
                attempts_done = int(row.get("publish_attempts") or 0)
                attempt_next = attempts_done + 1

                msg = _mk_message(row)
                key = str(row["aggregate_id"])  # IMPORTANT: keep per-aggregate ordering

                # hard validation -> permanent failure -> DLQ immediately
                if not msg.get("event_id") or not msg.get("type") or not msg.get("aggregate", {}).get("id"):
                    err = f"PermanentError: invalid envelope for outbox id={event_id}"
                    dlq_msg = dict(msg)
                    dlq_msg["dlq"] = {"failed_at": _utc_now_iso(), "attempts": attempt_next, "error": err}

                    try:
                        send_sync(producer, DLQ_TOPIC, key, dlq_msg, PUBLISH_TIMEOUT_SEC)
                    except Exception as e2:
                        # DLQ failed => retry later (do NOT deadlock the event)
                        delay = backoff_seconds(attempt_next)
                        with SessionLocal() as db:
                            with db.begin():
                                mark_retry(db, event_id=event_id, owner=OWNER,
                                           err=f"DLQ failed: {type(e2).__name__}: {e2}; original: {err}",
                                           delay_sec=delay)
                    else:
                        with SessionLocal() as db:
                            with db.begin():
                                mark_dead(db, event_id=event_id, owner=OWNER, err=f"DLQ: {err}")
                    continue

                # normal publish path
                try:
                    send_sync(producer, TOPIC, key, msg, PUBLISH_TIMEOUT_SEC)

                except Exception as e:
                    err = f"{type(e).__name__}: {e}"

                    # if attempts threshold reached -> try DLQ
                    if attempt_next >= MAX_ATTEMPTS:
                        dlq_msg = dict(msg)
                        dlq_msg["dlq"] = {"failed_at": _utc_now_iso(), "attempts": attempt_next, "error": err}

                        try:
                            send_sync(producer, DLQ_TOPIC, key, dlq_msg, PUBLISH_TIMEOUT_SEC)
                        except Exception as e2:
                            # DLQ failed => MUST retry (otherwise event freezes forever)
                            delay = backoff_seconds(attempt_next)
                            with SessionLocal() as db:
                                with db.begin():
                                    mark_retry(
                                        db,
                                        event_id=event_id,
                                        owner=OWNER,
                                        err=f"DLQ failed: {type(e2).__name__}: {e2}; original: {err}",
                                        delay_sec=delay,
                                    )
                        else:
                            with SessionLocal() as db:
                                with db.begin():
                                    mark_dead(db, event_id=event_id, owner=OWNER, err=f"DLQ: {err}")
                    else:
                        # retry main publish
                        delay = backoff_seconds(attempt_next)
                        with SessionLocal() as db:
                            with db.begin():
                                mark_retry(db, event_id=event_id, owner=OWNER, err=err, delay_sec=delay)

                else:
                    # success
                    with SessionLocal() as db:
                        with db.begin():
                            mark_sent(db, event_id=event_id, owner=OWNER)

    except KeyboardInterrupt:
        print("[relay] stopping...")

    finally:
        try:
            producer.flush(5)
            producer.close()
        except Exception:
            pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="readiness check (db + kafka tcp) and exit")
    args = ap.parse_args()

    if args.check:
        raise SystemExit(_check_mode())

    main()