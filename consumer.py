import json
import os
import signal
import time
import traceback
import uuid

from kafka import KafkaConsumer, KafkaProducer
from sqlalchemy import create_engine, text as sql_text
from sqlalchemy.orm import sessionmaker

# ---------------------------
# Config (env)
# ---------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/bot_game_test",
)
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:19092")
TOPIC = os.getenv("KAFKA_TOPIC", "game-events")
DLQ_TOPIC = os.getenv("KAFKA_DLQ_TOPIC", "game-events.dlq")
GROUP_ID = os.getenv("KAFKA_CONSUMER_GROUP", "game-consumer-v1")
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "10"))
IDLE_SLEEP_SEC = float(os.getenv("IDLE_SLEEP_SEC", "0.2"))

STOP = False


def _on_signal(sig, _frame):
    global STOP
    STOP = True
    print(f"[consumer] got signal {sig}, shutting down...", flush=True)


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


def safe_json_deserializer(b: bytes | None):
    """Return dict or None (never raise)."""
    if not b:
        return None
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return None


def _as_str_key(key):
    if key is None:
        return None
    if isinstance(key, bytes):
        return key.decode("utf-8", errors="ignore")
    return str(key)


def is_valid_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except Exception:
        return False


def already_consumed(db, *, event_id: str) -> bool:
    # event_id is UUID in schema
    res = db.execute(
        sql_text("SELECT 1 FROM consumed_events WHERE event_id = CAST(:event_id AS uuid)"),
        {"event_id": event_id},
    ).first()
    return res is not None


def mark_consumed(
    db,
    *,
    event_id: str,
    topic: str,
    partition: int,
    offset: int,
    aggregate_type: str | None,
    aggregate_id: str,
    event_type: str,
):
    """Idempotent write: one row per event_id.

    Works with both schemas:
      - consumed_events(kafka_offset)
      - consumed_events("offset")  (legacy)
    """

    params = {
        "event_id": event_id,
        "topic": topic,
        "partition": partition,
        "kafka_offset": offset,
        "offset": offset,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "event_type": event_type,
    }

    # Prefer the new column name.
    try:
        db.execute(
            sql_text(
                """
                INSERT INTO consumed_events
                  (event_id, topic, \"partition\", kafka_offset, aggregate_type, aggregate_id, event_type, consumed_at)
                VALUES
                  (CAST(:event_id AS uuid), :topic, :partition, :kafka_offset, :aggregate_type, CAST(:aggregate_id AS uuid), :event_type, now())
                ON CONFLICT (event_id) DO NOTHING
                """
            ),
            params,
        )
        return
    except Exception as e:
        # Fallback to legacy column name "offset" if needed.
        if "kafka_offset" not in str(e) and "does not exist" not in str(e):
            # Not a schema mismatch → re-raise.
            raise

    db.execute(
        sql_text(
            """
            INSERT INTO consumed_events
              (event_id, topic, \"partition\", \"offset\", aggregate_type, aggregate_id, event_type, consumed_at)
            VALUES
              (CAST(:event_id AS uuid), :topic, :partition, :offset, :aggregate_type, CAST(:aggregate_id AS uuid), :event_type, now())
            ON CONFLICT (event_id) DO NOTHING
            """
        ),
        params,
    )


def recompute_read_model(db, *, game_id: str):
    """Materialize read-model for the game. If the function is absent, degrade gracefully."""
    try:
        db.execute(sql_text("SELECT recompute_game_read_model(CAST(:game_id AS uuid))"), {"game_id": game_id})
    except Exception as e:
        # In demo mode we don't want to crash if the function isn't present.
        msg = str(e)
        if "recompute_game_read_model" in msg and "does not exist" in msg:
            print("[consumer] WARN: recompute_game_read_model() отсутствует — пропускаю материализацию", flush=True)
            return
        raise


def publish_dlq(
    dlq: KafkaProducer,
    *,
    topic: str,
    record,
    msg: dict,
    err: Exception,
    attempt: int,
    reason: str = "processing_error",
):
    payload = {
        "reason": reason,
        "attempt": attempt,
        "error": repr(err),
        "traceback": traceback.format_exc(),
        "original": msg,
        "src": {
            "topic": getattr(record, "topic", None),
            "partition": getattr(record, "partition", None),
            "offset": getattr(record, "offset", None),
            "key": _as_str_key(getattr(record, "key", None)),
        },
    }

    # Key must be str for our serializer.
    dlq_key = payload["src"]["key"]
    dlq.send(topic, key=dlq_key, value=payload)
    dlq.flush(5)


def _sleep_backoff(attempt: int):
    # bounded exponential backoff: 0.2, 0.4, 0.8, 1.6, 2.0, 2.0...
    time.sleep(min(0.2 * (2 ** max(0, attempt - 1)), 2.0))


def main():
    print(f"[consumer] bootstrap={KAFKA_BOOTSTRAP_SERVERS} topic={TOPIC} group={GROUP_ID}", flush=True)

    engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, future=True)

    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=safe_json_deserializer,
        key_deserializer=lambda b: b.decode("utf-8") if b else None,
        consumer_timeout_ms=1000,
    )

    dlq = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        key_serializer=lambda s: s.encode("utf-8") if s else None,
        value_serializer=lambda v: json.dumps(v, ensure_ascii=False).encode("utf-8"),
    )

    ok = dedup = skipped = dlq_cnt = errors = 0
    last_metrics = time.time()

    try:
        while not STOP:
            any_msg = False

            try:
                for record in consumer:
                    any_msg = True
                    if STOP:
                        break

                    msg = record.value
                    if msg is None:
                        skipped += 1
                        consumer.commit()
                        continue

                    event_id = msg.get("event_id")
                    event_type = msg.get("type") or msg.get("event_type") or "unknown"
                    aggregate = msg.get("aggregate") or {}
                    agg_type = aggregate.get("type")
                    agg_id = aggregate.get("id")

                    # Basic validation to avoid hard crashes on malformed messages.
                    if not is_valid_uuid(event_id) or not is_valid_uuid(agg_id):
                        skipped += 1
                        consumer.commit()
                        continue

                    attempt = 0
                    handled = False

                    while not handled and attempt < MAX_ATTEMPTS and not STOP:
                        try:
                            with Session.begin() as db:
                                if already_consumed(db, event_id=event_id):
                                    dedup += 1
                                    handled = True
                                    break

                                # demo: materialize by aggregate id (game id)
                                recompute_read_model(db, game_id=agg_id)

                                mark_consumed(
                                    db,
                                    event_id=event_id,
                                    topic=record.topic,
                                    partition=record.partition,
                                    offset=record.offset,
                                    aggregate_type=agg_type,
                                    aggregate_id=agg_id,
                                    event_type=event_type,
                                )

                            ok += 1
                            handled = True

                        except Exception as e:
                            attempt += 1
                            if attempt < MAX_ATTEMPTS:
                                _sleep_backoff(attempt)
                                continue

                            # Give up → DLQ + mark consumed to avoid infinite reprocessing in demo.
                            errors += 1
                            try:
                                publish_dlq(
                                    dlq,
                                    topic=DLQ_TOPIC,
                                    record=record,
                                    msg=msg,
                                    err=e,
                                    attempt=attempt,
                                    reason="processing_error",
                                )
                                dlq_cnt += 1
                            except Exception as dlq_e:
                                print(f"[consumer] ERROR: failed to publish DLQ: {dlq_e!r}", flush=True)

                            try:
                                with Session.begin() as db:
                                    mark_consumed(
                                        db,
                                        event_id=event_id,
                                        topic=record.topic,
                                        partition=record.partition,
                                        offset=record.offset,
                                        aggregate_type=agg_type,
                                        aggregate_id=agg_id,
                                        event_type=f"DLQ:{event_type}",
                                    )
                            except Exception as db_e:
                                print(f"[consumer] ERROR: failed to mark_consumed after DLQ: {db_e!r}", flush=True)

                            handled = True

                    consumer.commit()

            except Exception as loop_e:
                # If kafka connection hiccups, don't crash the container.
                print(f"[consumer] ERROR: consumer loop exception: {loop_e!r}", flush=True)
                _sleep_backoff(3)

            if not any_msg:
                time.sleep(IDLE_SLEEP_SEC)

            now = time.time()
            if now - last_metrics >= 10:
                print(
                    f"[metrics] ok={ok} dedup={dedup} skipped={skipped} dlq={dlq_cnt} errors={errors}",
                    flush=True,
                )
                last_metrics = now

    finally:
        try:
            dlq.flush(5)
        except Exception:
            pass
        try:
            dlq.close(5)
        except Exception:
            pass
        try:
            consumer.close(5)
        except Exception:
            pass


if __name__ == "__main__":
    main()
