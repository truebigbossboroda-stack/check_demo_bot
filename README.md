# Event-driven game backend demo (Outbox → Kafka)

This repository demonstrates:
- Transactional Outbox (PostgreSQL → Kafka)
- Idempotent consumer (dedup by event_id) + retry/backoff + DLQ
- Read-model materialization + replay/recompute
- Alembic migrations and integration tests for key guarantees

## Quickstart
1) Start infrastructure:
```bash
docker compose up -d

2) Install deps:

python -m venv .venv
# Windows:
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

3) Run migrations:

alembic upgrade head