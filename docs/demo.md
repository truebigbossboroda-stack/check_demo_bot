@"
# Демо: Outbox → Kafka → Consumer (PowerShell)

## Запуск
```powershell
docker compose up -d pg kafka kafka-ui
docker compose run --rm migrator
docker compose up -d relay consumer
docker compose ps