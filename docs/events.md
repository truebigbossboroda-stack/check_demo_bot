# Kafka Events Contract (v1)

## 1) Topics
- `game-events` — основной поток доменных событий
- `game-events.dlq` — dead-letter (invalid / unknown / poisoned)

## 2) Kafka Key
Инвариант: `key = aggregate.id` (UUID string). Key обязателен (не null).
Зачем: гарантируем ordering внутри агрегата и стабильный partitioning.

## 3) Event Envelope (value JSON)
Каждое сообщение — JSON envelope + доменный payload.

### Envelope schema (v1)
- `schema_version` (int) — версия схемы envelope+payload (контракт)
- `event_id` (uuid) — уникальный id события (дедуп на стороне consumer)
- `type` (string) — тип события (строго из whitelist)
- `aggregate.type` (string) — тип агрегата (например `game_session`)
- `aggregate.id` (uuid) — id агрегата
- `idempotency_key` (string|null) — идемпотентность команд/эффектов (опционально)
- `created_at` (RFC3339, UTC) — время создания события
- `payload` (object) — доменная нагрузка (зависит от type)

```json
{
  "schema_version": 1,
  "event_id": "2b37e2c4-3cc7-4b2c-8efb-6e1b4d56e3b8",
  "type": "game_session.created",
  "aggregate": { "type": "game_session", "id": "f1a9b9b4-5dc8-4d8e-9a4e-0c0c3d1a2f45" },
  "idempotency_key": "create_session:tg_user:12345",
  "created_at": "2026-02-05T12:00:00Z",
  "payload": {}
}