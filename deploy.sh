#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

MODE="${1:-full}"
COMPOSE_IMPL=""
POSTGRES_CONTAINER="telegramsellbot-postgres"

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

read_env_value() {
  local key="$1"
  local line
  line="$(grep -E "^${key}=" .env | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 0
  fi
  printf '%s' "${line#*=}"
}

wait_for_postgres() {
  local db_user db_name attempt max_attempts
  db_user="$(read_env_value POSTGRES_USER)"
  db_name="$(read_env_value POSTGRES_DB)"
  db_user="${db_user:-telegramsellbot}"
  db_name="${db_name:-telegramsellbot}"
  max_attempts=30

  for attempt in $(seq 1 "${max_attempts}"); do
    if docker exec "${POSTGRES_CONTAINER}" pg_isready -U "${db_user}" -d "${db_name}" >/dev/null 2>&1; then
      echo "PostgreSQL is ready."
      return 0
    fi
    echo "Waiting for PostgreSQL to become ready (${attempt}/${max_attempts})..."
    sleep 2
  done

  echo "PostgreSQL did not become ready in time." >&2
  return 1
}

quick_reload() {
  echo "Quick reloading api, bot, and worker services..."
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml restart api bot worker
}

full_deploy() {
  if [[ "${COMPOSE_IMPL}" == "legacy" ]]; then
    echo "Legacy docker-compose detected; applying compatibility cleanup before deploy..."
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml down --remove-orphans || true
    docker rm -f telegramsellbot-postgres telegramsellbot-redis telegramsellbot-api telegramsellbot-bot telegramsellbot-worker >/dev/null 2>&1 || true
  fi

  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d --build postgres redis
  wait_for_postgres

  DB_BOOTSTRAP_EXIT_CODE=0
  if [[ -f "alembic.ini" && -d "migrations" ]]; then
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml run --rm api python -m alembic upgrade head || DB_BOOTSTRAP_EXIT_CODE=$?
  else
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml run --rm api python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())" || DB_BOOTSTRAP_EXIT_CODE=$?
  fi

  if [[ "${DB_BOOTSTRAP_EXIT_CODE}" -ne 0 ]]; then
    if docker volume ls --format '{{.Name}}' | grep -q '^telegramsellbot_postgres_data$'; then
      echo
      echo "Database bootstrap failed while an existing PostgreSQL volume is present."
      echo "Most likely cause: POSTGRES_PASSWORD in .env no longer matches the password stored in the existing database volume."
      echo
      echo "If this is a fresh install and you do NOT need old data, run:"
      echo "  docker volume rm telegramsellbot_postgres_data"
      echo "Then rerun the installer."
      echo
      echo "If you need the old data, restore the original POSTGRES_PASSWORD and DATABASE_URL values in .env, then deploy again."
      echo
    fi
    exit "${DB_BOOTSTRAP_EXIT_CODE}"
  fi

  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d --build api bot worker
}

case "${MODE}" in
  full|--full)
    full_deploy
    ;;
  reload|--reload|restart|--restart)
    quick_reload
    ;;
  *)
    echo "Unknown deploy mode: ${MODE}. Supported modes: full, reload" >&2
    exit 1
    ;;
esac
