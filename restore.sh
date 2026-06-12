#!/bin/bash
# ============================================================================
#  TelegramSellBot — Restore
# ----------------------------------------------------------------------------
#  Restores from either:
#    * NEW comprehensive bundle (tar.gz) produced by backup.sh — restores
#      DB + .env + ready_configs + (optionally) X-UI panel DB files.
#    * LEGACY DB-only dump (.sql.gz or .sql) produced by older backup.sh —
#      restores ONLY the DB; operator brings their own .env.
#
#  Format is auto-detected from the file's tar header. The script
#  refuses to wipe a populated DB without an explicit "OVERWRITE"
#  confirmation, and it always snapshots the existing .env to
#  `.env.bak.<timestamp>` before replacing it.
# ============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
READY_CONFIGS_DIR="${PROJECT_DIR}/ready_configs"
POSTGRES_CONTAINER="telegramsellbot-postgres"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR!]${NC} $*" >&2; }

if [[ $# -lt 1 ]]; then
  cat <<EOF
Usage:
    ${0##*/} <backup_file>

Accepts:
    backups/tsb_backup_*.tar.gz           — comprehensive bundle (DB + .env + …)
    backups/telegramsellbot_backup_*.sql.gz  — legacy DB-only dump
EOF
  exit 1
fi

BACKUP_FILE="$1"
if [[ ! -f "${BACKUP_FILE}" ]]; then
  err "File not found: ${BACKUP_FILE}"
  exit 1
fi

read_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | sed -E "s/^${key}=//" || true
}

# ── Sanity: Postgres has to be alive ────────────────────────────────────
if ! docker ps --format '{{.Names}}' | grep -q "^${POSTGRES_CONTAINER}$"; then
  err "Postgres container '${POSTGRES_CONTAINER}' not running. Start the stack first."
  exit 1
fi

# ── Detect format ───────────────────────────────────────────────────────
FORMAT="legacy"
if tar -tzf "${BACKUP_FILE}" >/dev/null 2>&1; then
  # It's a tarball. Peek inside for the new-format manifest.
  if tar -tzf "${BACKUP_FILE}" | grep -q "^manifest.json$"; then
    FORMAT="bundle"
  fi
fi

echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}    TelegramSellBot — Restore${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
info "Source:  ${BACKUP_FILE}"
info "Format:  ${FORMAT}"

# ── Staging dir + extraction ────────────────────────────────────────────
STAGE="$(mktemp -d -t tsb_restore.XXXXXX)"
trap 'rm -rf "${STAGE}"' EXIT

if [[ "${FORMAT}" == "bundle" ]]; then
  info "Extracting bundle…"
  tar -xzf "${BACKUP_FILE}" -C "${STAGE}"
  if [[ ! -f "${STAGE}/db.sql.gz" ]]; then
    err "bundle is missing db.sql.gz — refusing to proceed"
    exit 1
  fi
  if [[ -f "${STAGE}/manifest.json" ]]; then
    info "Manifest:"
    sed 's/^/    /' "${STAGE}/manifest.json"
  fi
fi

# ── .env handling (bundle path only) ────────────────────────────────────
BAK=""
if [[ "${FORMAT}" == "bundle" && -f "${STAGE}/env" ]]; then
  echo
  warn "Bundle contains a .env file."
  if [[ -f "${ENV_FILE}" ]]; then
    BAK="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    cp -p "${ENV_FILE}" "${BAK}"
    ok "Snapshotted current .env → ${BAK}"
  fi
  cp -p "${STAGE}/env" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  ok "Restored .env from bundle (mode 600)."
elif [[ "${FORMAT}" == "legacy" ]]; then
  warn "Legacy DB-only format — .env on this host is unchanged."
  warn "Make sure your APP_SECRET_KEY matches the one used when the dump was taken,"
  warn "otherwise encrypted X-UI panel passwords in the DB will decrypt to garbage."
fi

# ── Read DB creds from CURRENT .env ─────────────────────────────────────
DB_USER="$(read_env_value POSTGRES_USER)"; DB_USER="${DB_USER:-telegramsellbot}"
DB_NAME="$(read_env_value POSTGRES_DB)";   DB_NAME="${DB_NAME:-telegramsellbot}"

# Refuse unsafe names.
if [[ ! "${DB_NAME}" =~ ^[A-Za-z0-9_]+$ ]]; then
  err "POSTGRES_DB contains unsafe characters: ${DB_NAME}"
  exit 1
fi
if [[ ! "${DB_USER}" =~ ^[A-Za-z0-9_]+$ ]]; then
  err "POSTGRES_USER contains unsafe characters: ${DB_USER}"
  exit 1
fi

# ── Sync the volume's role password to the restored .env (bundle only) ──
# An initialised postgres volume keeps the password it was created with —
# the POSTGRES_PASSWORD env var is ignored on later boots. After the .env
# swap above (e.g. restoring on a NEW host that install.sh initialised
# with a random password), the restored password no longer matches the
# volume and api/bot/worker would all fail TCP auth after './deploy.sh
# full'. docker-exec psql rides on local trust auth, so the ALTER works
# regardless of the mismatch. Runs BEFORE the destructive steps below so
# a failure aborts with the DB untouched (and rolls the .env back).
if [[ "${FORMAT}" == "bundle" && -f "${STAGE}/env" ]]; then
  DB_PASSWORD="$(read_env_value POSTGRES_PASSWORD)"
  if [[ -n "${DB_PASSWORD}" ]]; then
    info "Syncing postgres role password to the restored .env…"
    PW_SQL=${DB_PASSWORD//\'/\'\'}   # double single quotes for the SQL literal
    if ! docker exec -i "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d postgres \
          -q -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE "${DB_USER}" WITH PASSWORD '${PW_SQL}';
SQL
    then
      err "Could not sync the postgres role password (does role '${DB_USER}' exist in this volume?)."
      err "Aborting BEFORE touching the database — nothing was wiped."
      err "همگام‌سازی رمز دیتابیس ناموفق بود؛ پیش از هر تغییری در دیتابیس، عملیات متوقف شد."
      if [[ -n "${BAK}" ]]; then
        cp -p "${BAK}" "${ENV_FILE}"
        warn "Rolled the previous .env back from ${BAK}."
        warn "فایل .env قبلی از نسخهٔ پشتیبان بازگردانده شد."
      fi
      exit 1
    fi
    ok "Role password synced — restored .env now matches the postgres volume."
  else
    warn "Restored .env has no POSTGRES_PASSWORD — role password left unchanged; deploy may fail to authenticate."
  fi
fi

# ── Validate the dump BEFORE any destructive step ───────────────────────
# Earlier versions dropped the database first and only then ran gunzip;
# a plain .sql input (documented as supported) or a corrupt/truncated
# .sql.gz made the script die AFTER the wipe, leaving an empty DB.
DB_FILE=""
if [[ "${FORMAT}" == "bundle" ]]; then
  DB_FILE="${STAGE}/db.sql.gz"
else
  DB_FILE="${BACKUP_FILE}"
fi

DB_READER=(gunzip -c)
if [[ "$(head -c 2 "${DB_FILE}" | od -An -tx1 | tr -d ' \n')" == "1f8b" ]]; then
  info "Validating gzip integrity of the dump…"
  if ! gzip -t "${DB_FILE}" 2>/dev/null; then
    err "Dump is gzip-compressed but corrupt/truncated: ${DB_FILE}"
    err "Aborting BEFORE touching the database — nothing was wiped."
    err "فایل پشتیبان خراب است؛ پیش از هر تغییری در دیتابیس، عملیات متوقف شد."
    exit 1
  fi
else
  # Not gzipped — treat as a plain .sql dump (legacy documented format).
  DB_READER=(cat)
fi

# Peek at the first few KB and make sure it actually looks like a
# PostgreSQL SQL dump, not some unrelated file. (`|| true` guards the
# expected SIGPIPE when `head` closes the stream early under pipefail.)
DUMP_HEAD="$( ("${DB_READER[@]}" "${DB_FILE}" 2>/dev/null || true) | head -c 4096 | tr -d '\0' )"
if ! printf '%s' "${DUMP_HEAD}" | grep -qiE 'PostgreSQL database dump|CREATE TABLE|CREATE DATABASE|INSERT INTO|COPY .* FROM stdin|SET statement_timeout'; then
  err "Input does not look like a PostgreSQL SQL dump: ${DB_FILE}"
  err "Aborting BEFORE touching the database — nothing was wiped."
  err "فایل انتخاب‌شده یک خروجی معتبر pg_dump نیست؛ دیتابیس دست‌نخورده باقی ماند."
  exit 1
fi
ok "Backup input validated (readable, looks like a pg_dump SQL script)."

# ── Confirm before wiping the DB ────────────────────────────────────────
TABLE_COUNT="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" -tAc \
    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
if [[ "${TABLE_COUNT:-0}" -gt 0 ]]; then
  echo
  warn "Target DB already has ${TABLE_COUNT} table(s) — restore will WIPE them."
  read -r -p "Type OVERWRITE to proceed, anything else aborts: " confirm
  if [[ "${confirm}" != "OVERWRITE" ]]; then
    warn "Aborted by operator. .env was already restored from bundle (if applicable)."
    exit 1
  fi
fi

# ── DB restore ──────────────────────────────────────────────────────────
# (DB_FILE and DB_READER were resolved + validated above, pre-wipe.)
info "Terminating active connections to ${DB_NAME}…"
docker exec "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d postgres \
    -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${DB_NAME}';" \
    >/dev/null 2>&1 || true

info "Dropping + recreating database…"
docker exec "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d postgres -c "DROP DATABASE IF EXISTS \"${DB_NAME}\";" >/dev/null
docker exec "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d postgres -c "CREATE DATABASE \"${DB_NAME}\";" >/dev/null

info "Restoring from ${DB_FILE}…"
"${DB_READER[@]}" "${DB_FILE}" | docker exec -i "${POSTGRES_CONTAINER}" psql -U "${DB_USER}" -d "${DB_NAME}" >/dev/null
ok "Database restored."

# ── ready_configs (bundle only) ─────────────────────────────────────────
if [[ "${FORMAT}" == "bundle" && -d "${STAGE}/ready_configs" ]]; then
  info "Restoring ready_configs/…"
  rm -rf "${READY_CONFIGS_DIR}"
  mv "${STAGE}/ready_configs" "${READY_CONFIGS_DIR}"
  ok "ready_configs/ restored"
fi

# ── X-UI panel dumps note ───────────────────────────────────────────────
if [[ "${FORMAT}" == "bundle" && -d "${STAGE}/xui_databases" ]]; then
  XCOUNT="$(find "${STAGE}/xui_databases" -maxdepth 1 -type f -name '*.db' | wc -l | tr -d ' ')"
  if [[ "${XCOUNT}" -gt 0 ]]; then
    ARCHIVED="${PROJECT_DIR}/backups/xui_databases_restored_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "${ARCHIVED}"
    cp -R "${STAGE}/xui_databases/." "${ARCHIVED}/"
    ok "Stored ${XCOUNT} X-UI panel DB file(s) in ${ARCHIVED}/ for manual re-upload."
    info "(Sanaei X-UI panels are restored from inside the panel UI, not by this script.)"
  fi
fi

# ── Restart hint ────────────────────────────────────────────────────────
echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
ok "Restore complete."
echo
echo -e "${DIM}Next steps:${NC}"
echo -e "  ${BOLD}./deploy.sh full${NC}    rebuild images + run schema migrations"
echo -e "  ${BOLD}./doctor.sh${NC}         verify everything is green"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
