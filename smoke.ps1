$ErrorActionPreference = "Stop"

function Fail($msg) {
  Write-Host "[FAIL] $msg" -ForegroundColor Red
  exit 1
}

function Ok($msg) {
  Write-Host "[OK] $msg" -ForegroundColor Green
}

function Step($msg) {
  Write-Host "`n== $msg ==" -ForegroundColor Cyan
}

# Параметры демо (можешь поменять)
$ChatId = -5056821738
$GameId = "11111111-1111-1111-1111-111111111111"

Step "0) Проверка Docker"
try {
  docker version | Out-Null
  Ok "Docker доступен"
} catch {
  Fail "Docker недоступен. Проверь, что Docker Desktop запущен."
}

Step "1) Поднимаем pg + kafka + kafka-ui"
docker compose up -d pg kafka kafka-ui | Out-Null

# Ждём, пока pg станет healthy
$maxWait = 60
$ok = $false
for ($i=0; $i -lt $maxWait; $i++) {
  $ps = docker compose ps --format json | Out-String
  if ($ps -match '"Service":"pg"' -and $ps -match '"Health":"healthy"') {
    $ok = $true
    break
  }
  Start-Sleep -Seconds 1
}
if (-not $ok) {
  docker compose ps
  Fail "pg не стал healthy за $maxWait секунд"
}
Ok "pg healthy, kafka/kafka-ui подняты"

Step "2) Прогоняем миграции (alembic upgrade head)"
try {
  docker compose run --rm migrator
  Ok "Миграции прошли"
} catch {
  Fail "Миграции упали. Смотри лог выше (Traceback/ошибка alembic)."
}

Step "3) Запускаем relay + consumer"
docker compose up -d relay consumer | Out-Null

# Проверим что оба контейнера Up
$psText = docker compose ps | Out-String
if ($psText -notmatch "relay" -or $psText -notmatch "Up") { docker compose ps; Fail "relay не в состоянии Up" }
if ($psText -notmatch "consumer" -or $psText -notmatch "Up") { docker compose ps; Fail "consumer не в состоянии Up" }
Ok "relay и consumer запущены (Up)"

Step "4) Вставляем событие в outbox_events и получаем (event_id, idempotency_key)"
$sqlInsert = @"
INSERT INTO outbox_events
(id,event_type,aggregate_type,aggregate_id,payload,created_at,publish_attempts,last_error,published_at,idempotency_key,status)
VALUES
(
  gen_random_uuid(),
  'game.finished',
  'game',
  '$GameId',
  jsonb_build_object('game_id','$GameId','chat_id',$ChatId),
  now(),
  0,
  NULL,
  NULL,
  concat('demo-', gen_random_uuid()),
  'new'
)
RETURNING (id::text || '|' || idempotency_key);
"@

try {
  $ret = $sqlInsert | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1 -qAt
} catch {
  Fail "INSERT в outbox_events не прошёл. Проверь схему БД и миграции."
}

if (-not $ret -or $ret -notmatch "\|") {
  Fail "Не удалось распарсить RETURNING из psql. Получено: $ret"
}

$parts = $ret.Trim().Split("|")
$EventId = $parts[0]
$IdemKey = $parts[1]

Ok ("INSERT OK: event_id={0}  idempotency_key={1}" -f $EventId, $IdemKey)

Step "5) Ждём, пока outbox_events станет sent (работа relay)"
$maxWait = 30
$sent = $false
for ($i=0; $i -lt $maxWait; $i++) {
  $q = @"
SELECT status
FROM outbox_events
WHERE id = '$EventId'::uuid;
"@
  $status = $q | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1 -qAt
  if ($status.Trim() -eq "sent") { $sent = $true; break }
  Start-Sleep -Seconds 1
}
if (-not $sent) {
  $q2 = @"
SELECT id, status, created_at, published_at, last_error
FROM outbox_events
WHERE id = '$EventId'::uuid;
"@
  $q2 | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1
  Fail "За $maxWait сек outbox_events не стал sent. Смотри relay logs: docker compose logs -n 200 relay"
}
Ok "outbox_events стал sent"

Step "6) Ждём запись в consumed_events (работа consumer)"
$maxWait = 45
$consumed = $false
for ($i=0; $i -lt $maxWait; $i++) {
  $q = @"
SELECT count(*)
FROM consumed_events
WHERE event_id = '$EventId'::uuid;
"@
  $cnt = $q | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1 -qAt
  if ([int]$cnt.Trim() -ge 1) { $consumed = $true; break }
  Start-Sleep -Seconds 1
}

if (-not $consumed) {
  Write-Host "`nПоследние логи consumer:" -ForegroundColor Yellow
  docker compose logs -n 200 consumer
  Fail "За $maxWait сек не появилась запись в consumed_events. Проверь consumer и настройки топика/группы."
}

Ok "consumed_events содержит запись для event_id"

Step "7) Печатаем строку consumed_events для наглядности"
$qShow = @"
SELECT event_id, event_type, topic, "partition", kafka_offset, consumed_at
FROM consumed_events
WHERE event_id = '$EventId'::uuid;
"@
$qShow | docker compose exec -T pg psql -U postgres -d bot_game_test -v ON_ERROR_STOP=1

Write-Host "`nSMOKE TEST PASSED ✅" -ForegroundColor Green
