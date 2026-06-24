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

  local api_id bot_id worker_id
  api_id="$("${COMPOSE_CMD[@]}" -f docker-compose.prod.yml ps -q api 2>/dev/null || true)"
  bot_id="$("${COMPOSE_CMD[@]}" -f docker-compose.prod.yml ps -q bot 2>/dev/null || true)"
  worker_id="$("${COMPOSE_CMD[@]}" -f docker-compose.prod.yml ps -q worker 2>/dev/null || true)"

  if [[ -n "${api_id}" && -n "${bot_id}" && -n "${worker_id}" ]]; then
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml restart api bot worker
  else
    echo "Some app containers do not exist yet; starting api, bot, and worker instead..."
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d api bot worker
  fi
}

full_deploy() {
  if [[ "${COMPOSE_IMPL}" == "legacy" ]]; then
    echo "Legacy docker-compose detected; applying compatibility cleanup before deploy..."
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml down --remove-orphans || true
    docker rm -f telegramsellbot-postgres telegramsellbot-redis telegramsellbot-api telegramsellbot-bot telegramsellbot-worker >/dev/null 2>&1 || true
  fi

  # ── 1. Bring up postgres + redis (data layer) ───────────────────────────
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml up -d --build postgres redis
  wait_for_postgres

  # ── 2. Build the api image BEFORE running any one-off task in it ────────
  #
  # init_database + migrations both run inside `compose run --rm api …`
  # which spins up an ephemeral container from the CURRENT api image.
  # If the image is stale (still pre-migration code), the ephemeral
  # container won't have today's `scripts/migrations/` directory and
  # the migration step crashes with:
  #
  #     python: can't open file '/app/scripts/migrations/001_…py':
  #     [Errno 2] No such file or directory
  #
  # Building api now — before init+migrations — guarantees the ephemeral
  # container is always built from the just-pulled code. Plus we ALSO
  # bind-mount scripts/ as a belt-and-braces fallback for the case
  # where a build cache decided nothing changed.
  echo "Building api image so migrations see today's code..."
  "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml build api

  # The mount path is reused by both init and migration invocations
  # below. Use a function so the call sites stay readable.
  api_oneshot() {
    "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml run --rm \
      -v "$(pwd)/scripts:/app/scripts:ro" \
      api "$@"
  }

  # ── 3. init_database (or alembic, if configured) ────────────────────────
  DB_BOOTSTRAP_EXIT_CODE=0
  if [[ -f "alembic.ini" && -d "migrations" ]]; then
    api_oneshot python -m alembic upgrade head || DB_BOOTSTRAP_EXIT_CODE=$?
  else
    api_oneshot python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())" || DB_BOOTSTRAP_EXIT_CODE=$?
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

  # ── 4. Run additive migrations ──────────────────────────────────────────
  # `init_database()` (SQLAlchemy create_all) only adds missing TABLES.
  # It does NOT add new COLUMNS to existing tables — which means a code
  # update that introduces a new column (e.g. lifetime_used_bytes) on an
  # already-populated DB causes UndefinedColumn crashes until someone
  # runs the matching migration script by hand.
  #
  # Loop through every Python file in scripts/migrations/ — each must be
  # idempotent (re-runnable, no-op when already applied). add_*.py-style
  # one-off scripts live there; the deploy pulls them in automatically
  # so the operator can never forget.
  #
  # NOTE: the repo-root migrations/*.sql files are NOT executed by any
  # deploy path — they are manual/operational history (docs/DATABASE.md).
  # Anything every install must converge on has to be mirrored as an
  # idempotent Python script here (e.g. 005_money_constraints_and_
  # payment_unique.py mirrors 011 + 012).
  if [[ -d "scripts/migrations" ]]; then
    echo "Cleaning up containers and dropping database locks for migrations..."
    docker compose -f "${COMPOSE_FILE}" stop api bot worker || true
    # Forcefully kill any dangling 'run' containers (like previous interrupted migrations) holding locks
    docker ps -q --filter "name=telegramsellbot-api-run" | xargs -r docker rm -f || true
    # Restart postgres to guarantee all active connections and locks are dropped
    docker compose -f "${COMPOSE_FILE}" restart postgres
    echo "Waiting for Postgres to accept connections..."
    
    shopt -s nullglob
    migrations=(scripts/migrations/*.py)
    shopt -u nullglob
    for migration in "${migrations[@]}"; do
      echo "Running migration: ${migration}"
      if ! api_oneshot python "${migration}"; then
        echo "Migration ${migration} failed — aborting deploy. Fix the issue and re-run." >&2
        exit 1
      fi
    done
  fi

  # ── 5. Bring up app containers (api is already built; bot+worker now) ──
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
