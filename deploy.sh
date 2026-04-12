#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

COMPOSE_IMPL=""

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
  COMPOSE_IMPL="plugin"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
  COMPOSE_IMPL="legacy"
else
  echo "Docker Compose is not installed. Please install docker compose plugin or docker-compose." >&2
  exit 1
fi

if [[ "${COMPOSE_IMPL}" == "legacy" ]]; then
  echo "Legacy docker-compose detected; applying compatibility cleanup before deploy..."
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml down --remove-orphans || true
  docker rm -f telegramsellbot-postgres telegramsellbot-redis telegramsellbot-api telegramsellbot-bot telegramsellbot-worker >/dev/null 2>&1 || true
fi

"${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d --build postgres redis

if [[ -f "alembic.ini" && -d "migrations" ]]; then
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml run --rm api python -m alembic upgrade head
else
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml run --rm api python -c "import asyncio; from core.database import init_database; asyncio.run(init_database())"
fi

"${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d --build api bot worker
