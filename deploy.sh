#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"
source "${PROJECT_DIR}/scripts/ops-common.sh"
MODE="${1:-full}"
case "${MODE}" in
  full|--full|reload|--reload|restart|--restart|check|--check) ;;
  *) echo "Usage: bash deploy.sh [full|reload|check]" >&2; exit 2 ;;
esac

STAGE="preflight"
APPLICATION_PAUSED=0
SCHEMA_STARTED=0
deploy_error() {
  local rc="$1" line="$2"
  echo "Deploy failed during ${STAGE} (line ${line}, exit ${rc})." >&2
  if (( APPLICATION_PAUSED && ! SCHEMA_STARTED )); then
    echo "Schema changes have not started; resuming the existing app containers." >&2
    compose start api bot worker autoheal || true
  elif (( SCHEMA_STARTED )); then
    echo "Schema changes may have been applied. No automatic database rollback was attempted; fix the error and rerun full deploy. Pre-deploy backups are in backups/." >&2
  fi
  exit "${rc}"
}
trap 'deploy_error "$?" "$LINENO"' ERR
trap 'deploy_error 130 "$LINENO"' INT TERM
ops_acquire_lock
command -v docker >/dev/null || { echo "Docker is not installed." >&2; exit 1; }
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "Docker Compose is not installed." >&2; exit 1
fi
compose() { "${COMPOSE_CMD[@]}" -f docker-compose.prod.yml "$@"; }
step() { STAGE="$1"; printf '\n==> %s\n' "${STAGE}"; }

[[ -f .env ]] || { echo "Configure .env first (installer option 4)." >&2; exit 1; }
docker info >/dev/null
# Do not print the expanded configuration, which includes secrets.
compose config --quiet
export TSB_BUILD_REVISION
TSB_BUILD_REVISION="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
HEALTH_TIMEOUT="${TSB_HEALTH_TIMEOUT:-180}"
[[ "${HEALTH_TIMEOUT}" =~ ^[1-9][0-9]{0,2}$ ]] && (( HEALTH_TIMEOUT <= 900 )) || {
  echo "TSB_HEALTH_TIMEOUT must be between 1 and 900 seconds." >&2; exit 1;
}
echo "Source revision: ${TSB_BUILD_REVISION}"

wait_for_postgres() {
  local attempt
  for ((attempt=1; attempt<=30; attempt++)); do
    if docker exec telegramsellbot-postgres sh -c 'exec pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1 \
      && [[ "$(docker inspect -f '{{.State.Health.Status}}' telegramsellbot-redis 2>/dev/null || true)" == healthy ]]; then
      return 0
    fi
    sleep 2
  done
  echo "PostgreSQL/Redis did not become ready within 60 seconds." >&2
  return 1
}

verify_apps() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) service status healthy all_ready revision
  while (( SECONDS < deadline )); do
    all_ready=1
    for service in api bot worker; do
      status="$(docker inspect -f '{{.State.Status}}' "telegramsellbot-${service}" 2>/dev/null || true)"
      healthy="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "telegramsellbot-${service}" 2>/dev/null || true)"
      if [[ "${status}" == exited || "${status}" == dead ]]; then
        echo "${service} exited. Inspect: docker logs --tail 100 telegramsellbot-${service}" >&2
        return 1
      fi
      [[ "${status}" == running && "${healthy}" == healthy ]] || all_ready=0
    done
    if (( all_ready )); then
      # Probe the DB-backed API and SPA, not just /docs.
      if docker exec telegramsellbot-api curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/healthz \
        && docker exec telegramsellbot-api curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:8000/dashboard/; then
        for service in api bot worker; do
          revision="$(docker inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "telegramsellbot-${service}")"
          echo "${service}: healthy, image revision ${revision}"
          if [[ "${MODE}" == full || "${MODE}" == --full || "${MODE}" == check || "${MODE}" == --check ]]; then
            [[ "${revision}" == "${TSB_BUILD_REVISION}" ]] || {
              echo "${service} is running a different image revision." >&2; return 1;
            }
          elif [[ "${revision}" != "${TSB_BUILD_REVISION}" ]]; then
            echo "Source differs from ${service}'s image. Use Update & Deploy to apply code changes."
          fi
        done
        return 0
      fi
    fi
    sleep 2
  done
  echo "Services were not healthy within ${HEALTH_TIMEOUT}s. Run installer option 7 or 9 for diagnostics." >&2
  return 1
}

backup_before_migration() {
  local backup_file stamp
  stamp="$(date -u +%Y%m%dT%H%M%S)"
  mkdir -p backups
  chmod 700 backups
  backup_file="$(mktemp "${PROJECT_DIR}/backups/predeploy-${stamp}.XXXXXX")"
  # Use the container's parsed environment; do not source or reparse .env.
  if ! docker exec telegramsellbot-postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -F p' | gzip > "${backup_file}"; then
    rm -f -- "${backup_file}"
    echo "Database backup failed; schema changes were not started." >&2
    return 1
  fi
  gzip -t "${backup_file}" || return 1
  mv -- "${backup_file}" "${backup_file}.sql.gz"
  cp .env "${backup_file}.env"
  chmod 600 "${backup_file}.env" "${backup_file}.sql.gz"
  echo "Pre-deploy database + environment snapshot: ${backup_file}.{sql.gz,env}"
}

full_deploy() {
  step "1/6 Build backend and dashboard (running services stay up during build)"
  compose build api
  compose run --rm --no-deps -T api test -s /app/dashboard/dist/index.html
  step "2/6 Start and check data services"
  compose up -d postgres redis
  wait_for_postgres
  step "3/6 Pause app services and save database + environment"
  APPLICATION_PAUSED=1
  compose stop autoheal api bot worker
  backup_before_migration
  step "4/6 Apply schema and migrations"
  SCHEMA_STARTED=1
  if [[ -f alembic.ini && -d migrations ]]; then
    compose run --rm --no-deps -T api python -m alembic upgrade head
  else
    compose run --rm --no-deps -T api python -c 'import asyncio; import models; from core.database import init_database; asyncio.run(init_database())'
  fi
  local migration
  shopt -s nullglob
  for migration in scripts/migrations/*.py; do
    echo "Running ${migration}"
    compose run --rm --no-deps -T api python "${migration}"
  done
  shopt -u nullglob
  step "5/6 Recreate application with the built image"
  compose up -d --no-build api bot worker
  step "6/6 Verify health, dashboard and image revision"
  verify_apps
  compose up -d --no-build autoheal
  printf '%s\n' "${TSB_BUILD_REVISION}" > .deployed-revision
  echo "Deployment verified: ${TSB_BUILD_REVISION}"
}

case "${MODE}" in
  full|--full) full_deploy ;;
  check|--check)
    echo "Configuration and Docker preflight passed. Checking running services..."
    verify_apps
    ;;
  *)
    step "Restart app services (source and dashboard are not rebuilt)"
    compose restart api bot worker
    verify_apps
    ;;
esac
