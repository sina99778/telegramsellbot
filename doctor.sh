#!/usr/bin/env bash
# ============================================================================
#  TelegramSellBot — Doctor
# ----------------------------------------------------------------------------
#  Self-diagnose + self-heal. Walks through a checklist of "things that
#  should be true on a healthy deployment" and, by default, fixes what it
#  can. Wired into install.sh menu (option 11).
#
#  Modes
#  -----
#    ./doctor.sh                # default: scan + auto-fix what's safe
#    ./doctor.sh --dry-run      # scan only, never modify anything
#    ./doctor.sh --quiet        # suppress per-check INFO lines
#
#  Exit codes
#  ----------
#    0  all healthy
#    1  one or more checks FAILED and could not auto-fix
#    2  one or more WARN-level findings (operator should review)
# ============================================================================
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
LOG_FILE="${PROJECT_DIR}/doctor.log"

# Containers we own.
C_API="telegramsellbot-api"
C_BOT="telegramsellbot-bot"
C_WORKER="telegramsellbot-worker"
C_PG="telegramsellbot-postgres"
C_REDIS="telegramsellbot-redis"
ALL_CONTAINERS=("${C_API}" "${C_BOT}" "${C_WORKER}" "${C_PG}" "${C_REDIS}")
APP_CONTAINERS=("${C_API}" "${C_BOT}" "${C_WORKER}")

# Modes (parsed from argv below).
AUTO_FIX=1     # default ON — user asked for find-AND-fix.
DRY_RUN=0
QUIET=0

# Counters — populated by each check_*.
PASS=0
FIXED=0
WARN=0
FAIL=0

# ─── colours ────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ─── logging ────────────────────────────────────────────────────────────────
log_to_file() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}" 2>/dev/null || true
}

info()   { [[ "${QUIET}" -eq 1 ]] || echo -e "${BLUE}[INFO]${NC} $*"; log_to_file "INFO  $*"; }
ok()     { echo -e "${GREEN}[ OK ]${NC} $*"; log_to_file "OK    $*"; PASS=$((PASS + 1)); }
fixed()  { echo -e "${CYAN}[FIX!]${NC} $*"; log_to_file "FIX   $*"; FIXED=$((FIXED + 1)); }
warn()   { echo -e "${YELLOW}[WARN]${NC} $*"; log_to_file "WARN  $*"; WARN=$((WARN + 1)); }
fail()   { echo -e "${RED}[FAIL]${NC} $*"; log_to_file "FAIL  $*"; FAIL=$((FAIL + 1)); }
section(){ echo; echo -e "${BOLD}${CYAN}── $* ──${NC}"; }

# ─── compose helper ────────────────────────────────────────────────────────
compose_cmd() {
  if docker compose version >/dev/null 2>&1; then echo "plugin"
  elif command -v docker-compose >/dev/null 2>&1; then echo "legacy"
  else echo "missing"
  fi
}

run_compose() {
  case "$(compose_cmd)" in
    plugin) docker compose -f "${COMPOSE_FILE}" "$@" ;;
    legacy) docker-compose -f "${COMPOSE_FILE}" "$@" ;;
    *) return 1 ;;
  esac
}

# Safe fix wrapper — honours --dry-run.
do_fix() {
  local description="$1"; shift
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "[dry-run] would: ${description}"
    return 1
  fi
  if [[ "${AUTO_FIX}" -ne 1 ]]; then
    warn "auto-fix disabled — skipping: ${description}"
    return 1
  fi
  info "applying fix: ${description}"
  "$@"
}

read_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | sed -E "s/^${key}=//" || true
}

# ============================================================================
#  CHECKS
# ============================================================================

check_docker_daemon() {
  if docker info >/dev/null 2>&1; then
    ok "Docker daemon is running"
    return 0
  fi
  fail "Docker daemon is NOT running"
  if do_fix "start docker via systemctl" systemctl start docker; then
    sleep 2
    if docker info >/dev/null 2>&1; then
      fixed "Docker daemon started"
      FAIL=$((FAIL - 1))   # un-count the previous fail since we fixed it
      return 0
    fi
  fi
  return 1
}

check_docker_compose() {
  case "$(compose_cmd)" in
    plugin)
      ok "Docker Compose plugin available"
      return 0 ;;
    legacy)
      warn "Only legacy docker-compose available — install the v2 plugin for better DX"
      return 0 ;;
    *)
      fail "Neither 'docker compose' nor 'docker-compose' is installed"
      return 1 ;;
  esac
}

check_env_file() {
  if [[ ! -f "${ENV_FILE}" ]]; then
    fail ".env file is missing — run install.sh option 4"
    return 1
  fi
  local mode
  mode="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || stat -f '%Lp' "${ENV_FILE}" 2>/dev/null || echo "")"
  if [[ "${mode}" != "600" ]]; then
    warn ".env permission is ${mode} (should be 600 — readable by anyone in the deploy group)"
    if do_fix "chmod 600 .env" chmod 600 "${ENV_FILE}"; then
      fixed ".env permission set to 600"
      WARN=$((WARN - 1))
    fi
  else
    ok ".env present + mode 600"
  fi

  # Flag any required secret that is still the placeholder.
  local bad=0
  for key in BOT_TOKEN POSTGRES_PASSWORD REDIS_PASSWORD APP_SECRET_KEY; do
    local v
    v="$(read_env_value "${key}")"
    if [[ -z "${v}" || "${v}" == "CHANGE_ME"* || "${v}" == *"CHANGE_ME_BASE64"* ]]; then
      fail ".env ${key} is missing or placeholder"
      bad=1
    fi
  done
  [[ "${bad}" -eq 0 ]] && ok ".env critical secrets look real"
  return 0
}

# Each container exists (created at least once).
check_containers_exist() {
  local missing=()
  for c in "${ALL_CONTAINERS[@]}"; do
    if ! docker inspect "${c}" >/dev/null 2>&1; then
      missing+=("${c}")
    fi
  done

  if [[ ${#missing[@]} -eq 0 ]]; then
    ok "All 5 containers exist"
    return 0
  fi
  fail "Missing containers: ${missing[*]}"
  if do_fix "docker compose up -d (without recreate) to spawn missing containers" \
       run_compose up -d --no-recreate; then
    sleep 3
    local still=()
    for c in "${missing[@]}"; do
      docker inspect "${c}" >/dev/null 2>&1 || still+=("${c}")
    done
    if [[ ${#still[@]} -eq 0 ]]; then
      fixed "All missing containers were created"
      FAIL=$((FAIL - 1))
    else
      fail "Still missing after fix: ${still[*]}"
    fi
  fi
}

# Each container is currently `running`.
check_containers_running() {
  local stopped=()
  for c in "${ALL_CONTAINERS[@]}"; do
    local status
    status="$(docker inspect -f '{{.State.Status}}' "${c}" 2>/dev/null || echo "missing")"
    if [[ "${status}" != "running" ]]; then
      stopped+=("${c}:${status}")
    fi
  done

  if [[ ${#stopped[@]} -eq 0 ]]; then
    ok "All 5 containers are running"
    return 0
  fi
  fail "Stopped containers: ${stopped[*]}"
  # Start each stopped one by name (cheaper than `up -d` on the whole stack).
  for entry in "${stopped[@]}"; do
    local name="${entry%%:*}"
    if do_fix "docker start ${name}" docker start "${name}"; then
      sleep 2
      local s2
      s2="$(docker inspect -f '{{.State.Status}}' "${name}" 2>/dev/null || echo "missing")"
      if [[ "${s2}" == "running" ]]; then
        fixed "Container ${name} is now running"
        FAIL=$((FAIL - 1))
      else
        fail "Container ${name} would not start (status=${s2})"
      fi
    fi
  done
}

# Detect crash loops — high RestartCount with short uptime.
check_restart_loops() {
  local hits=0
  for c in "${ALL_CONTAINERS[@]}"; do
    local restarts uptime_s started_at
    restarts="$(docker inspect -f '{{.RestartCount}}' "${c}" 2>/dev/null || echo 0)"
    started_at="$(docker inspect -f '{{.State.StartedAt}}' "${c}" 2>/dev/null || echo "")"
    if [[ -z "${started_at}" ]]; then continue; fi
    # Compute uptime in seconds (best-effort; ignore parsing errors).
    uptime_s="$(( $(date +%s) - $(date -d "${started_at}" +%s 2>/dev/null || echo 0) ))"
    if [[ "${restarts}" -gt 5 && "${uptime_s}" -lt 600 ]]; then
      warn "${c} has restarted ${restarts}× in the last $((uptime_s/60))m — likely a crash loop"
      info "  → check logs: docker logs --tail=200 ${c}"
      hits=$((hits + 1))
    fi
  done
  [[ "${hits}" -eq 0 ]] && ok "No restart loops detected"
}

check_postgres_health() {
  local hs
  hs="$(docker inspect -f '{{.State.Health.Status}}' "${C_PG}" 2>/dev/null || echo "none")"
  if [[ "${hs}" == "healthy" ]]; then
    ok "Postgres healthcheck OK"
    return 0
  fi
  # Try directly — health may simply be "starting" / "none".
  if docker exec "${C_PG}" pg_isready -q 2>/dev/null; then
    ok "Postgres responds to pg_isready (compose health=${hs})"
    return 0
  fi
  fail "Postgres is not accepting connections (health=${hs})"
  if do_fix "restart postgres" docker restart "${C_PG}"; then
    sleep 6
    if docker exec "${C_PG}" pg_isready -q 2>/dev/null; then
      fixed "Postgres is back"
      FAIL=$((FAIL - 1))
    else
      fail "Postgres still not responding after restart"
    fi
  fi
}

check_redis_health() {
  local hs
  hs="$(docker inspect -f '{{.State.Health.Status}}' "${C_REDIS}" 2>/dev/null || echo "none")"
  if [[ "${hs}" == "healthy" ]]; then
    ok "Redis healthcheck OK"
    return 0
  fi
  # Direct probe (requires password from .env).
  local pw
  pw="$(read_env_value REDIS_PASSWORD)"
  if [[ -n "${pw}" ]] && docker exec "${C_REDIS}" redis-cli -a "${pw}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
    ok "Redis responds to PING (compose health=${hs})"
    return 0
  fi
  fail "Redis is not responding (health=${hs})"
  if do_fix "restart redis" docker restart "${C_REDIS}"; then
    sleep 4
    if docker exec "${C_REDIS}" redis-cli -a "${pw}" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
      fixed "Redis is back"
      FAIL=$((FAIL - 1))
    else
      fail "Redis still not responding"
    fi
  fi
}

# Verify the lifetime_used_bytes migration has been applied. Auto-runs it
# if the column is missing.
check_db_schema() {
  local pg_user pg_db col
  pg_user="$(read_env_value POSTGRES_USER)"; pg_user="${pg_user:-telegramsellbot}"
  pg_db="$(read_env_value POSTGRES_DB)";     pg_db="${pg_db:-telegramsellbot}"

  col="$(docker exec "${C_PG}" psql -U "${pg_user}" -d "${pg_db}" -tAc \
      "SELECT 1 FROM information_schema.columns WHERE table_name='subscriptions' AND column_name='lifetime_used_bytes'" 2>/dev/null \
      | tr -d '[:space:]')"

  if [[ "${col}" == "1" ]]; then
    ok "DB schema: lifetime_used_bytes column present"
    return 0
  fi

  fail "DB schema: lifetime_used_bytes column is MISSING (causes total-volume under-reporting)"
  if do_fix "run scripts/add_lifetime_used_bytes.py via api container" \
       run_compose run --rm \
         -v "${PROJECT_DIR}/scripts:/app/scripts:ro" \
         api python scripts/add_lifetime_used_bytes.py; then
    # Re-check.
    sleep 1
    col="$(docker exec "${C_PG}" psql -U "${pg_user}" -d "${pg_db}" -tAc \
        "SELECT 1 FROM information_schema.columns WHERE table_name='subscriptions' AND column_name='lifetime_used_bytes'" 2>/dev/null \
        | tr -d '[:space:]')"
    if [[ "${col}" == "1" ]]; then
      fixed "Column added + backfill complete"
      FAIL=$((FAIL - 1))
      # The api/bot/worker have been retrying queries against the
      # now-missing column and emitting UndefinedColumn errors. Restart
      # them so (a) the next log scan doesn't surface stale errors and
      # (b) any in-memory SQLAlchemy column-cache is reset.
      info "Restarting app containers to clear stale UndefinedColumn errors..."
      docker restart "${C_API}" "${C_BOT}" "${C_WORKER}" >/dev/null 2>&1 || true
      sleep 4
    else
      fail "Migration ran but column still missing — check 'docker logs api'"
    fi
  fi
}

# Check that bot's heartbeat file (inside container) was touched in the last 60s.
check_bot_heartbeat() {
  if ! docker inspect "${C_BOT}" >/dev/null 2>&1; then return; fi
  local age
  age="$(docker exec "${C_BOT}" sh -c \
      'test -f /tmp/bot_heartbeat && echo $(( $(date +%s) - $(stat -c %Y /tmp/bot_heartbeat) ))' \
      2>/dev/null || echo "999999")"
  if [[ "${age}" =~ ^[0-9]+$ && "${age}" -lt 60 ]]; then
    ok "Bot heartbeat fresh (${age}s ago)"
    return 0
  fi
  fail "Bot heartbeat is stale (${age}s ago) — dispatcher is likely hung"
  if do_fix "restart bot" docker restart "${C_BOT}"; then
    sleep 8
    age="$(docker exec "${C_BOT}" sh -c \
        'test -f /tmp/bot_heartbeat && echo $(( $(date +%s) - $(stat -c %Y /tmp/bot_heartbeat) ))' \
        2>/dev/null || echo "999999")"
    if [[ "${age}" =~ ^[0-9]+$ && "${age}" -lt 60 ]]; then
      fixed "Bot heartbeat is back (${age}s ago)"
      FAIL=$((FAIL - 1))
    else
      fail "Bot heartbeat still stale after restart"
    fi
  fi
}

# Worker has a slower beat (every 30s), tolerate up to 180s.
check_worker_heartbeat() {
  if ! docker inspect "${C_WORKER}" >/dev/null 2>&1; then return; fi
  local age
  age="$(docker exec "${C_WORKER}" sh -c \
      'test -f /tmp/worker_heartbeat && echo $(( $(date +%s) - $(stat -c %Y /tmp/worker_heartbeat) ))' \
      2>/dev/null || echo "999999")"
  if [[ "${age}" =~ ^[0-9]+$ && "${age}" -lt 180 ]]; then
    ok "Worker heartbeat fresh (${age}s ago)"
    return 0
  fi
  fail "Worker heartbeat is stale (${age}s ago) — scheduler is likely hung"
  if do_fix "restart worker" docker restart "${C_WORKER}"; then
    sleep 10
    age="$(docker exec "${C_WORKER}" sh -c \
        'test -f /tmp/worker_heartbeat && echo $(( $(date +%s) - $(stat -c %Y /tmp/worker_heartbeat) ))' \
        2>/dev/null || echo "999999")"
    if [[ "${age}" =~ ^[0-9]+$ && "${age}" -lt 180 ]]; then
      fixed "Worker heartbeat is back (${age}s ago)"
      FAIL=$((FAIL - 1))
    else
      fail "Worker heartbeat still stale after restart"
    fi
  fi
}

check_api_http() {
  if ! docker inspect "${C_API}" >/dev/null 2>&1; then return; fi
  # Try the in-container /healthz endpoint (more reliable than the host's
  # /docs check — /healthz also probes the DB).
  if docker exec "${C_API}" sh -c "curl -fsS --max-time 5 http://127.0.0.1:8000/healthz >/dev/null" 2>/dev/null; then
    ok "API /healthz responds with 200"
    return 0
  fi
  fail "API /healthz is not responding"
  if do_fix "restart api" docker restart "${C_API}"; then
    sleep 8
    if docker exec "${C_API}" sh -c "curl -fsS --max-time 5 http://127.0.0.1:8000/healthz >/dev/null" 2>/dev/null; then
      fixed "API /healthz is back"
      FAIL=$((FAIL - 1))
    else
      fail "API /healthz still not responding after restart"
    fi
  fi
}

check_disk_space() {
  local avail_mb
  avail_mb="$(df -m "${PROJECT_DIR}" | awk 'NR==2 {print $4}')"
  if [[ -z "${avail_mb}" ]]; then
    warn "Could not read disk space"
    return
  fi
  if [[ "${avail_mb}" -lt 500 ]]; then
    fail "Only ${avail_mb} MB free on the project partition — image builds will fail"
  elif [[ "${avail_mb}" -lt 2048 ]]; then
    warn "Low disk space: ${avail_mb} MB free (recommend ≥ 2 GB)"
  else
    ok "Disk space healthy (${avail_mb} MB free)"
  fi
}

# Scan recent logs for known error patterns. No auto-fix — output is
# meant to point the operator at root cause.
#
# We use `docker logs --since=10m` instead of `--tail=N` so an old
# error from BEFORE today's fix doesn't keep showing up as a fresh
# WARN every time the operator runs the doctor.
check_log_errors() {
  declare -A patterns=(
    ["MissingGreenlet"]="Async-lazy-load bug — file should eager-load via selectinload"
    ["psycopg.errors|asyncpg.exceptions"]="PostgreSQL error — likely schema or connection"
    ["redis.exceptions.ConnectionError"]="Redis unreachable — check container + .env REDIS_PASSWORD"
    ["TelegramRetryAfter"]="Bot is being rate-limited by Telegram — usually self-recovers"
    ["TelegramServerError"]="Telegram side hiccup — usually transient"
    ["UndefinedColumn"]="DB schema drift — run doctor again to apply pending migrations"
    ["ImportError|ModuleNotFoundError"]="Missing dependency — \`docker compose build\` to rebuild image"
    ["X-UI panel error|XUI .* failed"]="Reseller panel unreachable — check XUI_BASE_URL + credentials"
  )
  local any_hits=0
  for c in "${APP_CONTAINERS[@]}"; do
    if ! docker inspect "${c}" >/dev/null 2>&1; then continue; fi
    # `--since=10m` filters to entries from the last 10 minutes only.
    # 2>&1 captures stderr from the container too, but stderr from
    # `docker logs` itself (e.g. "no configuration" if a context is
    # weird) goes to /dev/null so it doesn't pollute the log buffer.
    local log
    log="$(docker logs --since=10m "${c}" 2>&1 < /dev/null || true)"
    for pat in "${!patterns[@]}"; do
      local count
      count="$(printf '%s\n' "${log}" | grep -cE "${pat}" || true)"
      if [[ "${count}" -gt 0 ]]; then
        warn "${c}: ${count}× '${pat}' (last 10 min) — ${patterns[${pat}]}"
        any_hits=$((any_hits + 1))
      fi
    done
  done
  [[ "${any_hits}" -eq 0 ]] && ok "No known error patterns in the last 10 minutes of logs"
}

# Compare local code to origin/master. Don't auto-pull — that's the
# `Update & Deploy` action in install.sh.
check_code_freshness() {
  if [[ ! -d "${PROJECT_DIR}/.git" ]] || ! command -v git >/dev/null 2>&1; then
    return
  fi
  if ! git -C "${PROJECT_DIR}" fetch --quiet origin master 2>/dev/null; then
    warn "Could not 'git fetch origin master' — check repo state"
    return
  fi

  # Production should always be on the master branch. If the local
  # checkout is on a feature branch, an older `Update & Deploy` could
  # have silently rolled the server backward to that branch's stale
  # remote pointer. Flag it loudly.
  local branch
  branch="$(git -C "${PROJECT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")"
  if [[ "${branch}" != "master" && "${branch}" != "HEAD" ]]; then
    warn "Local branch is '${branch}' — production should be on 'master'. Run install.sh option 1 to switch."
  fi

  local behind
  behind="$(git -C "${PROJECT_DIR}" rev-list HEAD..origin/master --count 2>/dev/null || echo 0)"
  if [[ "${behind}" -gt 0 ]]; then
    warn "Local code is ${behind} commit(s) behind origin/master — run install.sh option 1"
  else
    ok "Code is up to date with origin/master"
  fi
}

# ============================================================================
#  MAIN
# ============================================================================

usage() {
  cat <<EOF
TelegramSellBot doctor — self-diagnose + self-heal

Usage:
  ${0##*/}              scan and auto-fix (default)
  ${0##*/} --dry-run    scan only, never modify
  ${0##*/} --quiet      suppress INFO lines, only show ok/warn/fail/fix
  ${0##*/} --help       this message

Exit codes:  0 healthy   1 unfixed FAIL   2 WARN-level findings only
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; AUTO_FIX=0 ;;
    --quiet)   QUIET=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown flag: $1"; usage; exit 2 ;;
  esac
  shift
done

echo -e "${BOLD}${CYAN}╔═══════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${CYAN}║${NC}  ${BOLD}TelegramSellBot Doctor${NC}                          ${BOLD}${CYAN}║${NC}"
echo -e "${BOLD}${CYAN}╚═══════════════════════════════════════════════════╝${NC}"
mode_label="auto-fix"
[[ "${DRY_RUN}" -eq 1 ]] && mode_label="${YELLOW}dry-run (no changes)${NC}"
echo -e "  ${DIM}mode: ${mode_label}${NC}  •  ${DIM}log: ${LOG_FILE}${NC}"

section "1. Prerequisites"
check_docker_daemon || { echo; fail "Cannot proceed without Docker"; exit 1; }
check_docker_compose
check_env_file

section "2. Containers"
check_containers_exist
check_containers_running
check_restart_loops

section "3. Data layer"
check_postgres_health
check_redis_health
check_db_schema

section "4. Application health"
check_api_http
check_bot_heartbeat
check_worker_heartbeat

section "5. Host"
check_disk_space

section "6. Logs"
check_log_errors

section "7. Code freshness"
check_code_freshness

# ─── summary ────────────────────────────────────────────────────────────────
echo
echo -e "${BOLD}━━━━━━━━━━━━━━ summary ━━━━━━━━━━━━━━${NC}"
printf "  ${GREEN}%-8s${NC} %d\n"  "passed:" "${PASS}"
printf "  ${CYAN}%-8s${NC} %d\n"   "fixed:"  "${FIXED}"
printf "  ${YELLOW}%-8s${NC} %d\n" "warn:"   "${WARN}"
printf "  ${RED}%-8s${NC} %d\n"    "failed:" "${FAIL}"
echo

if [[ "${FAIL}" -gt 0 ]]; then
  echo -e "${RED}Some checks could not be auto-fixed. Run install.sh option 6 to view logs.${NC}"
  exit 1
fi
if [[ "${WARN}" -gt 0 ]]; then
  echo -e "${YELLOW}Healthy with warnings. Review the [WARN] lines above.${NC}"
  exit 2
fi
echo -e "${GREEN}✅ Everything is healthy.${NC}"
exit 0
