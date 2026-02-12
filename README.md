# Demo: Transactional Outbox → Kafka → Consumer (PostgreSQL)

Этот репозиторий — небольшой **демо‑кейс “Transactional Outbox”**:
1) приложение пишет событие в `outbox_events` (в одной транзакции с доменными данными),
2) **relay** (`outbox_publisher.py`) публикует событие в **Kafka**,
3) **consumer** (`consumer.py`) читает Kafka и фиксирует факт обработки в **PostgreSQL** (`consumed_events`) + (при желании) обновляет read‑model.

Цель: чтобы работодатель мог **поднять всё одной командой** и увидеть полный цикл “DB → Kafka → DB”.

---

## Что внутри

- **PostgreSQL 16** (Docker)
- **Apache Kafka 3.7.0** (Docker)
- **Kafka UI** (Docker) — веб‑интерфейс на `http://localhost:8088`
- **Alembic migrations** — создают таблицы/вьюхи, включая:
  - `outbox_events` — очередь событий на публикацию
  - `consumed_events` — журнал обработанных событий (dedup / exactly-once на уровне consumer)
- **relay**: `outbox_publisher.py` — читает `outbox_events`, отправляет в Kafka и переводит запись в `sent`
- **consumer**: `consumer.py` — читает Kafka и пишет факт обработки в `consumed_events`

---

## Быстрый старт (Windows PowerShell)

### 0) Требования
- Docker Desktop
- PowerShell 5+ / PowerShell 7+
- Порты свободны: **5432**, **19092**, **8088**

Проверка:
```powershell
docker version
docker compose version
```

---

### 1) Поднять инфраструктуру (pg + kafka + kafka-ui)
```powershell
docker compose up -d pg kafka kafka-ui
docker compose ps
```

**Ожидаемый итог:**
- `pg` = `healthy`
- `kafka` и `kafka-ui` = `Up`
- Kafka UI открывается: `http://localhost:8088`

---

### 2) Прогнать миграции
```powershell
docker compose run --rm migrator
```

**Ожидаемый итог:** в логах `alembic` есть `upgrade ... -> head` и команда завершается без ошибок.

Проверка таблиц:
```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;"
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT table_name FROM information_schema.views WHERE table_schema='public' ORDER BY 1;"
```

---

### 3) Запустить relay + consumer

> Важно: **consumer** собирается как Docker image.  
> Если вы меняли `consumer.py`, запускайте с `--build`, иначе можно случайно поднять старую версию.

```powershell
docker compose up -d --build relay consumer
docker compose ps
```

**Ожидаемый итог:**
- `relay` = `Up`
- `consumer` = `Up`

Проверка логов:
```powershell
docker compose logs -n 50 relay
docker compose logs -n 50 consumer
```

---

### 4) Вставить событие в outbox_events (правильный JSON без экранирования)

Самый надёжный способ — `jsonb_build_object`, чтобы не ловить ошибки кавычек.

```powershell
$sql = @"
INSERT INTO outbox_events
(id,event_type,aggregate_type,aggregate_id,payload,created_at,publish_attempts,last_error,published_at,idempotency_key,status)
VALUES
(
  gen_random_uuid(),
  'game.finished',
  'game',
  '11111111-1111-1111-1111-111111111111',
  jsonb_build_object('game_id','11111111-1111-1111-1111-111111111111','chat_id',-5056821738),
  now(),
  0,
  NULL,
  NULL,
  concat('demo-', gen_random_uuid()),
  'new'
);
"@

$sql | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1
```

**Ожидаемый итог:** `INSERT 0 1`

Можно посмотреть последнюю запись:
```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT id, status, created_at, idempotency_key FROM outbox_events ORDER BY created_at DESC LIMIT 5;"
```

---

### 5) Убедиться, что relay перевёл событие в sent

```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT id, status, publish_attempts, last_error, published_at FROM outbox_events ORDER BY created_at DESC LIMIT 1;"
```

**Ожидаемый итог:** `status = sent`, `published_at` заполнен.

---

### 6) Убедиться, что consumer записал обработку в consumed_events

```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT event_id, event_type, topic, partition, kafka_offset, consumed_at FROM consumed_events ORDER BY consumed_at DESC NULLS LAST LIMIT 10;"
```

**Ожидаемый итог:** появляется строка(и) по `game-events` и `game.finished`.

---

## Smoke test (одной командой)

Если в репозитории есть `smoke.ps1`, можно запускать так:
```powershell
powershell -ExecutionPolicy Bypass -File .\smoke.ps1
```

**Ожидаемый итог:** все шаги `[OK]`, на шаге 6 появляется запись в `consumed_events`.

Если smoke падает на шаге 6 и в логах есть `NameError`/`TypeError` — почти всегда это **старый образ consumer**.
Решение:
```powershell
docker compose build consumer
docker compose up -d --force-recreate consumer
docker compose logs -n 80 consumer
```

---

## Replay (повторное чтение Kafka)

По умолчанию consumer делает dedup через `consumed_events`. Для “повторного прогона” в демо безопаснее всего **сменить consumer group**:

```powershell
$env:KAFKA_CONSUMER_GROUP = "game-consumer-replay-$(Get-Date -Format 'yyyyMMddHHmmss')"
docker compose up -d --force-recreate --no-deps consumer
docker compose logs -n 30 consumer
```

**Ожидаемый итог:** consumer начинает читать топик “с нуля” для новой группы.

---

## Troubleshooting

### 1) Ошибка JSON в INSERT
Если видите `invalid input syntax for type json` — используйте `jsonb_build_object` (см. шаг 4) или `$sql` here‑string.

### 2) consumer падает, но relay переводит outbox в sent
Проверьте логи:
```powershell
docker compose logs -n 120 consumer
```

Типовые причины:
- забыли `--build` после правок в `consumer.py`
- неверные переменные окружения (в Docker должны быть `pg:5432` и `kafka:19092`)

### 3) Локальный запуск consumer.py (без Docker)
Локально нужно явно задать env:
```powershell
$env:DATABASE_URL = "postgresql://postgres:postgres@localhost:5432/bot_game_test"
$env:KAFKA_BOOTSTRAP_SERVERS = "localhost:19092"
python consumer.py
```
В демо для работодателя рекомендую **всё через Docker**, так стабильнее.

---

## Как залить на GitHub (коротко)

1) Проверить, что всё работает:
```powershell
git status
powershell -ExecutionPolicy Bypass -File .\smoke.ps1
```

2) Добавить изменения и проверить, что именно коммитится:
```powershell
git add .
git diff --staged
```

3) Коммит:
```powershell
git commit -m "docs: add runbook + smoke demo"
```

4) Пуш:
```powershell
git remote add origin <URL_ВАШЕГО_REPO>
git branch -M main
git push -u origin main
```

**Ожидаемый итог:** в GitHub появляется README, docker-compose, скрипты и инструкции запуска.
