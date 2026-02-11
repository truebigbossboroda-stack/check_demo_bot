import os
import json
import time
import traceback
import signal
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from kafka import KafkaConsumer, KafkaProducer
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text as sql_text


load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing")

BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
TOPIC = os.getenv("KAFKA_TOPIC", "game-events")
GROUP_ID = os.getenv("KAFKA_CONSUMER_GROUP") or os.getenv("KAFKA_GROUP_ID") or "game-consumer-v1"
DLQ_TOPIC = os.getenv("KAFKA_DLQ_TOPIC", f"{TOPIC}.dlq")
MAX_ATTEMPTS = int(os.getenv("KAFKA_MAX_ATTEMPTS", "5"))          # сколько раз пробуем обработать сообщение
BASE_BACKOFF_SEC = float(os.getenv("KAFKA_BACKOFF_SEC", "0.5"))   # базовая задержка
METRICS_EVERY_SEC = float(os.getenv("KAFKA_METRICS_EVERY_SEC", "10"))

MATERIALIZE_TYPES = {
    "game.created",
    "player.joined",
    "phase.changed",
    "round.started",
    "round.resolved",
    "game.finished",
    "game.archived",
    "snapshot.created",
}

engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_payload_dict(msg: dict) -> dict:
    payload = msg.get("payload") or {}
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload


def mark_consumed(db, *, event_id, topic: str, partition: int, offset: int, aggregate_type, aggregate_id, event_type):
    db.execute(
        sql_text("""
            INSERT INTO consumed_events (id, event_id, topic, "partition", "offset", aggregate_type, aggregate_id, event_type)
            VALUES (gen_random_uuid(), CAST(:event_id AS uuid), :topic, :partition, :offset, :aggregate_type, CAST(:aggregate_id AS uuid), :event_type)
            ON CONFLICT (event_id) DO NOTHING
        """),
        {
            "event_id": event_id,
            "topic": topic,
            "partition": partition,
            "offset": offset,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
        },
    )

def already_consumed(db, event_id) -> bool:
    r = db.execute(
        sql_text("SELECT 1 FROM consumed_events WHERE event_id = CAST(:event_id AS uuid)"),
        {"event_id": event_id},
    ).first()
    return r is not None

STOP = False

def _handle_stop(signum, frame):
    global STOP
    STOP = True

# !!! ВАЖНО: это заглушка materialization.
# Замени на реальный UPSERT под твою game_read_model.
def upsert_read_model_stub(db, *, chat_id: int | None, game_id: str, msg: dict):
    # Требует, чтобы в game_read_model была колонка state_json jsonb и уникальность по game_id.
    # Если у тебя другое — пришли DDL, я перепишу.
    db.execute(
        sql_text("""
            INSERT INTO game_read_model (game_id, chat_id, state_json, updated_at)
            VALUES (CAST(:game_id AS uuid), :chat_id, CAST(:state AS jsonb), now())
            ON CONFLICT (game_id) DO UPDATE
              SET chat_id = EXCLUDED.chat_id,
                  state_json = EXCLUDED.state_json,
                  updated_at = now()
        """),
        {
            "game_id": game_id,
            "chat_id": chat_id,
            "state": json.dumps(msg, ensure_ascii=False),
        },
    )


def safe_json_deserializer(b: bytes):
    # Kafka может прислать tombstone (value=None) или пустой payload
    if b is None:
        return None
    s = b.decode("utf-8", errors="ignore").strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        # мусор/не-json => игнорируем
        return None

def recompute_read_model(db, *, game_id: str):
    db.execute(
        sql_text("SELECT recompute_game_read_model(CAST(:game_id AS uuid))"),
        {"game_id": game_id},
    )

def publish_dlq(dlq, *, topic: str, record, msg: dict | None, err: Exception, attempt: int, reason: str):
    payload = {
        "dlq_version": 1,
        "reason": reason,
        "failed_at": utc_now_iso(),
        "attempt": attempt,
        "error_type": type(err).__name__,
        "error": str(err),
        "traceback": traceback.format_exc(limit=20),
        "src": {
            "topic": getattr(record, "topic", None),
            "partition": getattr(record, "partition", None),
            "offset": getattr(record, "offset", None),
            "timestamp": getattr(record, "timestamp", None),
            "key": (record.key.decode("utf-8", errors="ignore") if getattr(record, "key", None) else None),
        },
        "message": msg,   # если удалось распарсить в dict — можно передать сюда, иначе None
    }

    dlq.send(topic, key=(payload["src"]["key"] or None), value=payload)
    dlq.flush(timeout=10)

def main():
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=GROUP_ID,
        enable_auto_commit=False,     # commit только после DB commit
        auto_offset_reset="earliest",
        value_deserializer=safe_json_deserializer,
        key_deserializer=lambda b: b.decode("utf-8") if b else None,
        consumer_timeout_ms=1000,
    )
    

    dlq = KafkaProducer(
        bootstrap_servers=BOOTSTRAP,
        value_serializer=lambda d: json.dumps(d, ensure_ascii=False).encode("utf-8"),
        key_serializer=lambda s: (s.encode("utf-8") if s else None),
    )
       
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    print(f"[consumer] bootstrap={BOOTSTRAP} topic={TOPIC} group={GROUP_ID}")

    ok = 0
    dedup = 0
    skipped = 0
    dlq_cnt = 0
    err_cnt = 0
    last_metrics = time.time()

    try:
        while not STOP:
            any_msg = False
            for record in consumer:
                any_msg = True
                msg = record.value
                if msg is None:
                    skipped += 1
                    consumer.commit()
                    continue

                event_type = msg.get("type")
                if event_type not in MATERIALIZE_TYPES:
                    skipped += 1
                    consumer.commit()
                    continue

                event_id = msg.get("event_id")
                aggregate = msg.get("aggregate") or {}
                aggregate_id = aggregate.get("id")

                if not event_id or not aggregate_id:
                    skipped += 1
                    consumer.commit()
                    continue

                # retry loop
                handled = False
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        with SessionLocal() as db:
                            with db.begin():
                                if already_consumed(db, event_id):
                                    dedup += 1
                                else:
                                    recompute_read_model(db, game_id=aggregate_id)
                                
                                    mark_consumed(
                                        db,
                                        event_id=event_id,
                                        topic=record.topic,
                                        partition=record.partition,
                                        offset=record.offset,
                                        aggregate_type=aggregate.get("type"),
                                        aggregate_id=aggregate_id,
                                        event_type=event_type,
                                    )
                                    ok += 1

                        # DB commit прошёл -> коммитим offset в Kafka
                        consumer.commit()
                        handled = True
                        break

                    except Exception as e:
                        err_cnt += 1
                        # backoff (0.5, 1, 2, 4, ...)
                        if attempt < MAX_ATTEMPTS:
                            time.sleep(BASE_BACKOFF_SEC * (2 ** (attempt - 1)))
                            continue

                        # poison-message: в DLQ + помечаем consumed + commit offset
                        publish_dlq(dlq, topic=DLQ_TOPIC, record=record, msg=msg, err=e, attempt=attempt)

                        with SessionLocal() as db:
                            with db.begin():
                                # важно: чтобы не блокировать consumer навсегда на одном сообщении
                                if not already_consumed(db, event_id):
                                    mark_consumed(
                                        db,
                                        event_id=event_id,
                                        topic=record.topic,
                                        partition=record.partition,
                                        offset=record.offset,
                                        aggregate_type=aggregate.get("type"),
                                        aggregate_id=aggregate_id,
                                        event_type=f"DLQ:{event_type}",
                                    )
                        consumer.commit()
                        dlq_cnt += 1
                        handled = True
                        break

                # safety
                if not handled:
                    # теоретически не должен сюда попасть
                    consumer.commit()
            now = time.time()

            if now - last_metrics >= METRICS_EVERY_SEC:
                print(f"[metrics] ok={ok} dedup={dedup} skipped={skipped} dlq={dlq_cnt} errors={err_cnt}")
                last_metrics = now

            try:
                dlq.flush(timeout=10)
            finally:
                try:
                    dlq.close(timeout=10)
                except Exception:
                    pass
                try:
                    consumer.close()
                except Exception:
                    pass

            if not any_msg:
                time.sleep(0.2)
    finally:
        try:
            dlq.flush(timeout=10)
        except Exception:
            pass
        try:
            dlq.close(timeout=10)
        except Exception:
            pass
        try:
            consumer.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()