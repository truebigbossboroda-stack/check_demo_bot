import json
from typing import Any, Dict, Optional
from sqlalchemy import text as sql_text


# В v1 контракта: делаем idempotency_key обязательным для всех событий,
# которые могут повторно эмититься (а у вас почти все такие).
# Минимум: admin.* + базовые доменные события.
MUST_HAVE_IDEM_PREFIXES = ("admin.",)
MUST_HAVE_IDEM_TYPES = {
    "game.created",
    "player.joined",
    "phase.changed",
    "round.started",
    "round.resolved",
    "snapshot.created",
    "game.finished",
    "game.archived",
}

def _requires_idem(event_type: str) -> bool:
    if event_type.startswith(MUST_HAVE_IDEM_PREFIXES):
        return True
    return event_type in MUST_HAVE_IDEM_TYPES


def emit_event(
    db,
    *,
    event_type: str,
    aggregate_type: str,
    aggregate_id,
    payload: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> None:
    if payload is None:
        payload = {}

    # HARD GUARD
    if _requires_idem(event_type) and not idempotency_key:
        raise ValueError(f"idempotency_key is required for event_type={event_type}")

    db.execute(
        sql_text("""
            INSERT INTO outbox_events
                (event_type, aggregate_type, aggregate_id, payload, idempotency_key)
            VALUES
                (:event_type, :aggregate_type, :aggregate_id, CAST(:payload AS jsonb), :idempotency_key)
            ON CONFLICT (idempotency_key)
            WHERE idempotency_key IS NOT NULL
            DO NOTHING
        """),
        {
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": str(aggregate_id),
            "payload": json.dumps(payload, ensure_ascii=False),
            "idempotency_key": idempotency_key,
        },
    )