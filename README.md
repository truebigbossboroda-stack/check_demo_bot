# Demo: Transactional Outbox → Kafka → Consumer (PostgreSQL)

Небольшой демонстрационный проект, который показывает **полный цикл доставки событий** по паттерну **Transactional Outbox**:

1) приложение пишет событие в таблицу `outbox_events` **в той же транзакции**, что и доменные изменения;  
2) **relay** (`outbox_publisher.py`) читает `outbox_events`, публикует событие в **Kafka** и помечает запись как `sent`;  
3) **consumer** (`consumer.py`) читает Kafka и фиксирует факт обработки в **PostgreSQL** (`consumed_events`) — это база для **dedup/идемпотентности** на стороне потребителя.

Цель репозитория —  **поднять демо локально** и проверить, что событие реально прошло путь **DB → Kafka → DB**.

---

## Что внутри

- PostgreSQL 16 (Docker)
- Kafka (Docker) + Kafka UI (`http://localhost:8088`)
- Alembic migrations (создают схему БД: `outbox_events`, `consumed_events`, вьюхи)
- Relay: `outbox_publisher.py` (публикация outbox → Kafka + статусы `new/processing/sent`)
- Consumer: `consumer.py` (чтение Kafka, dedup, DLQ)

---

## Архитектура

- **Outbox в БД** гарантирует, что событие не “потеряется” между доменной транзакцией и Kafka.
- **Consumer пишет журнал обработанных сообщений** (`consumed_events`) и тем самым избегает повторной обработки при replay/перезапусках.

---

## Быстрый старт (рекомендуемый)

В репозитории есть `smoke.ps1`, запускайте так (PowerShell):

```powershell
powershell -ExecutionPolicy Bypass -File .\smoke.ps1
```

### Ожидаемый итог

- шаги 0–5 помечаются как `[OK]`;
- на шаге 6 появляется запись в `consumed_events`;
- в конце видите метрики consumer (например: `[metrics] ok=1 ...`).

Если на шаге 6 получаете `[FAIL]`, сначала смотрите логи `consumer` (см. раздел Troubleshooting).

---

## Ручной запуск (если хочется видеть каждый шаг)

### 0) Требования

- Docker Desktop
- PowerShell 5+ / PowerShell 7+
- Свободные порты: **5432**, **19092**, **8088**

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
- Kafka UI доступен на `http://localhost:8088`

---

### 2) Прогнать миграции

```powershell
docker compose run --rm migrator
```

**Ожидаемый итог:** в логах alembic идут `Running upgrade ...` до `head` и команда завершается без ошибок.

Проверка таблиц/вьюх:
```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;"
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT table_name FROM information_schema.views WHERE table_schema='public' ORDER BY 1;"
```

---

### 3) Запустить relay + consumer

Если вы меняли `consumer.py`, запускайте **с пересборкой** (иначе может подняться старый образ):

```powershell
docker compose up -d --build relay consumer
docker compose ps
```

Логи:
```powershell
docker compose logs -n 80 relay
docker compose logs -n 80 consumer
```

---

### 4) Вставить событие в outbox_events

JSON лучше формировать через `jsonb_build_object`, чтобы не бороться с кавычками и экранированием.

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

Проверить “верх” outbox:
```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT id, status, created_at, idempotency_key FROM outbox_events ORDER BY created_at DESC LIMIT 5;"
```

---

### 5) Убедиться, что relay пометил запись как sent

```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT id, status, publish_attempts, last_error, published_at FROM outbox_events ORDER BY created_at DESC LIMIT 1;"
```

**Ожидаемый итог:** `status = sent`, `published_at` не `NULL`.

---

### 6) Убедиться, что consumer записал обработку в consumed_events

```powershell
docker compose exec -T pg psql -U postgres -d bot_game_test -c "SELECT event_id, event_type, topic, partition, kafka_offset, consumed_at FROM consumed_events ORDER BY consumed_at DESC NULLS LAST LIMIT 10;"
```

**Ожидаемый итог:** появляется строка с `topic=game-events`, `event_type=game.finished`.

---

## Replay (повторное чтение Kafka)

Consumer делает dedup через таблицу `consumed_events`. Для повторного прогона демонстрации безопаснее всего **сменить consumer group**:

```powershell
$env:KAFKA_CONSUMER_GROUP = "game-consumer-replay-$(Get-Date -Format 'yyyyMMddHHmmss')"
docker compose up -d --force-recreate --no-deps consumer
docker compose logs -n 30 consumer
```

---

## Дополнительно: Telegram-игра NeMonopolia (main.py)

**NeMonopolia** — Telegram-бот (backend-логика + данные), из практического проекта под который применялся outbox‑подход.  
Игра хранит и изменяет состояние в PostgreSQL, ведёт аудит/снапшоты и эмитит доменные события в `outbox_events`, которые дальше идут по пайплайну **PostgreSQL → Outbox → Kafka → Consumer → PostgreSQL**.

Что этот кейс показывает дополнительно:

- доменную модель и state machine игры: фазы/раунды `lobby → income → event → world_arena → negotiations → orders → resolve → finished`;
- контроль конкурентных изменений при апдейте состояния (row-level lock на запись игры);
- аудит действий (кто/что/когда) + снапшоты состояния по ключевым шагам (воспроизводимость/разбор инцидентов);
- эмиссию доменных событий через outbox (`emit_event`) с `idempotency_key` для безопасной публикации/интеграции.

### Быстрый запуск игры (5–10 минут)

> Перед запуском бота подними инфраструктуру и сервисы outbox: раздел **«Ручной запуск» → шаги 1–3** (pg + kafka + kafka-ui, миграции, relay + consumer).

1) **Создай Telegram-бота** через BotFather и получи токен.

2) **Настрой `.env`** :

> ⚙️ Варианты подключений:
> - если запускаем `python main.py` **на хосте** (рекомендуемо для демо), оставь как в `.env.example`: `DATABASE_URL=...@localhost:5432/...`, `KAFKA_BOOTSTRAP_SERVERS=localhost:19092`
> - если запускаем бота **внутри docker compose**, поменяй на: `DATABASE_URL=...@pg:5432/...`, `KAFKA_BOOTSTRAP_SERVERS=kafka:19092`

```powershell
Copy-Item .env.example .env
# затем добавь строку:
# TELEGRAM_BOT_TOKEN=xxxxx
```

Linux/Mac:
```bash
cp .env.example .env
# затем добавь строку:
# TELEGRAM_BOT_TOKEN=xxxxx
```

3) **Установи зависимости**:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Linux/Mac:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4) **Запусти бота**:
```bash
python main.py
```

### Мини-сценарий в Telegram

В чате с ботом:
- `/start` — проверка, что бот жив

В групповом чате (куда добавлен бот):
- `/startgame` — создать игру
- `/joingame` — игрокам вступить
- `/ready` — подтвердить готовность на фазе
- `/begin_round` — ведущий запускает раунд
- `/next_phase` — переход по фазам (или `/menu` для меню)
- `/status`, `/gameinfo` — посмотреть статус
- `/orders`, `/rules` — меню указов/правила
- `/endgame` — завершить игру  
- (опционально) `/votum`, `/votum_result` — вотум недоверия

### Как убедиться, что outbox работает на событиях игры

- Логи:
```bash
docker compose logs -f relay consumer
```

- SQL‑проверки:
```sql
SELECT id, event_type, status, published_at, idempotency_key
FROM outbox_events
ORDER BY created_at DESC
LIMIT 20;

SELECT event_id, event_type, kafka_offset, created_at
FROM consumed_events
ORDER BY created_at DESC
LIMIT 20;
```

- Kafka UI: открой `http://localhost:8088` → topic `game-events` → messages.

## Troubleshooting

### consumer не пишет в consumed_events

1) Посмотреть логи:
```powershell
docker compose logs -n 200 consumer
```

2) Частая причина — изменили `consumer.py`, но не пересобрали образ:
```powershell
docker compose up -d --build --force-recreate consumer
```

3) Важно: **внутри Docker** используются адреса `pg:5432` и `kafka:19092`.  
Если случайно указать `localhost` внутри контейнера — соединение не будет работать.

---

### Ошибка JSON при INSERT

Если видите `invalid input syntax for type json`, значит сломались кавычки/экранирование.  
Решение: используйте `jsonb_build_object` (см. шаг 4).

---

### Полный сброс окружения

Если нужно начать “с чистого листа” (удалить volume с БД):

```powershell
docker compose down -v
```

## Статус
Демо‑проект для демонстрации паттерна Transactional Outbox и практики Docker/Kafka/PostgreSQL.
