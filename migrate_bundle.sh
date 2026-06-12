#!/usr/bin/env bash
# ============================================================================
#  TelegramSellBot — one-button server-migration bundle.
# ----------------------------------------------------------------------------
#  The previous backup story was ALREADY good for the database alone (see
#  backup.sh + apps/worker/jobs/backup.py — both schedule encrypted DB
#  dumps to admin DMs). What it WASN'T good for: lifting the entire bot
#  onto a new VPS in a single, foolproof step.
#
#  Why not?
#    * .env is intentionally excluded from backup.sh ("secrets in env
#      are not backed up"). Fair, but operators then had to manually
#      copy APP_SECRET_KEY across — and without that exact key, every
#      Fernet-encrypted X-UI panel password in the DB decrypts to
#      garbage on the new server.
#    * ready_configs/, redis volume, custom static files — none of
#      these have a single hand-off path.
#
#  This script bridges that gap with two operations:
#
#      ./migrate_bundle.sh create [--out PATH] [--passphrase-file F]
#         → One file: `tsb_migration_<timestamp>.tar.gz.enc` containing
#           DB dump + .env + ready_configs + manifest, AES-256-CBC
#           encrypted with an operator-supplied passphrase.
#
#      ./migrate_bundle.sh restore <bundle.tar.gz.enc> [--passphrase-file F]
#         → Validates passphrase, extracts, restores the DB (after a
#           safety confirmation if the target DB is already populated),
#           reinstates .env (with a sibling .env.bak.<ts> kept aside),
#           then prints next steps.
#
#  Encryption: openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt
#  -base64. No keys are stored on disk; the passphrase lives only in
#  the operator's head (and optionally in a temporary file passed via
#  --passphrase-file for unattended use).
# ============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
READY_CONFIGS_DIR="${PROJECT_DIR}/ready_configs"
BACKUPS_DIR="${PROJECT_DIR}/backups"

POSTGRES_CONTAINER="telegramsellbot-postgres"

# ── colours ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()     { echo -e "${RED}[ERR!]${NC} $*" >&2; }

usage() {
  cat <<EOF
${BOLD}TelegramSellBot — Migration bundle${NC}

USAGE
    ${0##*/} create   [--out FILE]   [--passphrase-file F]
    ${0##*/} restore  BUNDLE_FILE    [--passphrase-file F]

CREATE
    Builds a single encrypted file ready for transport to a new server.
    Contents: PostgreSQL dump + .env + ready_configs/ + manifest.
    Default output path: backups/tsb_migration_<timestamp>.tar.gz.enc

RESTORE
    Decrypts a bundle, restores the DB, and reinstates .env on the new
    server. Existing .env is preserved as .env.bak.<timestamp> in case
    the operator wants to roll back. The target DB is wiped only after
    typing 'OVERWRITE' at the safety prompt.

PASSPHRASE
    Asked at the terminal interactively. For automation, pass
    --passphrase-file pointing at a file whose first line is the
    passphrase (mode 600 strongly recommended).

EOF
}

# ─────────────────────────────────────────────────────────────────────────
#  helpers
# ─────────────────────────────────────────────────────────────────────────

read_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 | sed -E "s/^${key}=//" || true
}

read_env_value_from() {
  # Like read_env_value, but reads from an arbitrary env file (e.g. the
  # bundle's staged .env instead of the host's).
  local file="$1" key="$2"
  [[ -f "${file}" ]] || return 0
  grep -E "^${key}=" "${file}" | tail -n 1 | sed -E "s/^${key}=//" || true
}

ask_passphrase() {
  # Echoes the passphrase to stdout. Sources, in order: --passphrase-file,
  # env var BUNDLE_PASSPHRASE, interactive terminal prompt.
  if [[ -n "${PASSPHRASE_FILE:-}" ]]; then
    if [[ ! -r "${PASSPHRASE_FILE}" ]]; then
      err "passphrase file not readable: ${PASSPHRASE_FILE}"
      return 1
    fi
    head -n 1 "${PASSPHRASE_FILE}"
    return 0
  fi
  if [[ -n "${BUNDLE_PASSPHRASE:-}" ]]; then
    printf '%s' "${BUNDLE_PASSPHRASE}"
    return 0
  fi
  local p1 p2
  read -r -s -p "Passphrase: " p1; echo >&2
  if [[ "${1:-}" == "--confirm" ]]; then
    read -r -s -p "Passphrase (again): " p2; echo >&2
    if [[ "${p1}" != "${p2}" ]]; then
      err "passphrases do not match"
      return 1
    fi
  fi
  if [[ -z "${p1}" ]]; then
    err "empty passphrase"
    return 1
  fi
  printf '%s' "${p1}"
}

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
    *) err "docker compose not installed"; return 1 ;;
  esac
}

require_postgres_running() {
  if ! docker inspect "${POSTGRES_CONTAINER}" >/dev/null 2>&1; then
    err "Postgres container ${POSTGRES_CONTAINER} not found. Start the stack first."
    return 1
  fi
  if ! docker exec "${POSTGRES_CONTAINER}" pg_isready -q 2>/dev/null; then
    err "Postgres not ready. Start the stack and retry."
    return 1
  fi
}

# ─────────────────────────────────────────────────────────────────────────
#  CREATE
# ─────────────────────────────────────────────────────────────────────────

cmd_create() {
  local out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --out) out="$2"; shift 2 ;;
      --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
      *) err "unknown flag: $1"; usage; exit 2 ;;
    esac
  done

  if [[ ! -f "${ENV_FILE}" ]]; then
    err ".env not found at ${ENV_FILE}. Cannot build a complete bundle."
    exit 1
  fi
  require_postgres_running

  local ts
  ts="$(date +%Y%m%d_%H%M%S)"
  if [[ -z "${out}" ]]; then
    mkdir -p "${BACKUPS_DIR}"
    chmod 700 "${BACKUPS_DIR}"
    out="${BACKUPS_DIR}/tsb_migration_${ts}.tar.gz.enc"
  fi

  info "Reading passphrase…"
  local passphrase
  passphrase="$(ask_passphrase --confirm)"

  # Staging dir — wiped on exit.
  local stage
  stage="$(mktemp -d -t tsb_migrate.XXXXXX)"
  trap 'rm -rf "${stage}"' EXIT

  # 1. DB dump
  info "Dumping PostgreSQL…"
  local db_user db_name
  db_user="$(read_env_value POSTGRES_USER)"; db_user="${db_user:-telegramsellbot}"
  db_name="$(read_env_value POSTGRES_DB)";   db_name="${db_name:-telegramsellbot}"
  docker exec "${POSTGRES_CONTAINER}" pg_dump -U "${db_user}" -d "${db_name}" -F p \
    | gzip > "${stage}/db.sql.gz"
  ok "DB dump written ($(du -h "${stage}/db.sql.gz" | awk '{print $1}'))"

  # 2. .env (the critical piece backup.sh skips)
  info "Including .env…"
  cp -p "${ENV_FILE}" "${stage}/.env"

  # 3. ready_configs/ if present
  if [[ -d "${READY_CONFIGS_DIR}" ]]; then
    info "Including ready_configs/…"
    cp -R "${READY_CONFIGS_DIR}" "${stage}/ready_configs"
  fi

  # 4. Manifest — version + timestamp + repo SHA — so the restore step
  #    can sanity-check what it's about to write.
  local sha branch
  sha="$(git -C "${PROJECT_DIR}" rev-parse --short HEAD 2>/dev/null || echo "unknown")"
  branch="$(git -C "${PROJECT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "unknown")"
  cat > "${stage}/manifest.json" <<EOF
{
  "format_version": 1,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "git_branch": "${branch}",
  "git_sha": "${sha}",
  "includes": {
    "db_dump": true,
    "env": true,
    "ready_configs": $( [[ -d "${READY_CONFIGS_DIR}" ]] && echo "true" || echo "false" )
  }
}
EOF

  # 5. Tar + encrypt + write out.
  info "Packing + encrypting…"
  local plaintext="${stage}/bundle.tar.gz"
  ( cd "${stage}" && tar -czf "${plaintext}" db.sql.gz .env manifest.json \
      $( [[ -d "${stage}/ready_configs" ]] && echo "ready_configs" || true ) )

  # AES-256-CBC + PBKDF2 with 200k iterations. base64-armoured so the
  # file can be safely uploaded to Telegram / pasted in chat etc.
  openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -base64 \
    -in "${plaintext}" -out "${out}" -pass stdin <<< "${passphrase}"
  chmod 600 "${out}"

  echo
  ok "Bundle ready:"
  echo -e "  ${BOLD}${out}${NC}"
  echo -e "  size: $(du -h "${out}" | awk '{print $1}')"
  echo
  echo -e "${YELLOW}TRANSFER:${NC}  scp / Telegram / cloud — anything"
  echo -e "${YELLOW}DECRYPT:${NC}  remember the passphrase. Without it the bundle is junk."
  echo -e "${YELLOW}NEW HOST:${NC} clone the repo there, copy this file in, then run:"
  echo -e "             ${BOLD}./migrate_bundle.sh restore ${out##*/}${NC}"
}

# ─────────────────────────────────────────────────────────────────────────
#  RESTORE
# ─────────────────────────────────────────────────────────────────────────

cmd_restore() {
  local bundle=""
  if [[ $# -lt 1 ]]; then
    err "Usage: ${0##*/} restore BUNDLE_FILE [--passphrase-file F]"
    exit 2
  fi
  bundle="$1"; shift
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --passphrase-file) PASSPHRASE_FILE="$2"; shift 2 ;;
      *) err "unknown flag: $1"; exit 2 ;;
    esac
  done

  if [[ ! -f "${bundle}" ]]; then
    err "bundle file not found: ${bundle}"
    exit 1
  fi

  info "Reading passphrase…"
  local passphrase
  passphrase="$(ask_passphrase)"

  local stage
  stage="$(mktemp -d -t tsb_restore.XXXXXX)"
  trap 'rm -rf "${stage}"' EXIT

  info "Decrypting…"
  local plaintext="${stage}/bundle.tar.gz"
  if ! openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -base64 \
        -in "${bundle}" -out "${plaintext}" -pass stdin <<< "${passphrase}" 2>/dev/null; then
    err "decryption failed — wrong passphrase or corrupted bundle"
    exit 1
  fi
  ok "Decrypted ($(du -h "${plaintext}" | awk '{print $1}'))"

  info "Unpacking…"
  ( cd "${stage}" && tar -xzf "${plaintext}" )
  if [[ ! -f "${stage}/db.sql.gz" || ! -f "${stage}/.env" || ! -f "${stage}/manifest.json" ]]; then
    err "bundle is missing required parts (db.sql.gz / .env / manifest.json)"
    exit 1
  fi

  echo
  info "Bundle manifest:"
  cat "${stage}/manifest.json" | sed 's/^/    /'
  echo

  # ── Read DB identities from BOTH .envs ──
  # The postgres volume keeps the credentials it was initialised with (the
  # CURRENT host's .env); the bundle's .env carries the OLD server's
  # credentials. Every psql call below therefore connects as the volume's
  # existing role (docker-exec local trust auth); the bundle's password is
  # synced into that role only after the restore succeeds.
  local volume_db_user bundle_db_user bundle_db_name bundle_db_password
  volume_db_user="$(read_env_value POSTGRES_USER)"; volume_db_user="${volume_db_user:-telegramsellbot}"
  bundle_db_user="$(read_env_value_from "${stage}/.env" POSTGRES_USER)"; bundle_db_user="${bundle_db_user:-telegramsellbot}"
  bundle_db_name="$(read_env_value_from "${stage}/.env" POSTGRES_DB)";   bundle_db_name="${bundle_db_name:-telegramsellbot}"
  bundle_db_password="$(read_env_value_from "${stage}/.env" POSTGRES_PASSWORD)"

  if [[ ! "${bundle_db_name}" =~ ^[A-Za-z0-9_]+$ ]]; then
    err "Bundle POSTGRES_DB contains unsafe characters: ${bundle_db_name}"
    exit 1
  fi
  if [[ ! "${bundle_db_user}" =~ ^[A-Za-z0-9_]+$ ]]; then
    err "Bundle POSTGRES_USER contains unsafe characters: ${bundle_db_user}"
    exit 1
  fi

  require_postgres_running

  # Abort BEFORE touching anything if the bundle's DB role does not exist
  # in this postgres volume — after the .env swap the stack could never
  # authenticate, and fixing that needs an operator decision.
  if [[ "${bundle_db_user}" != "${volume_db_user}" ]]; then
    local role_exists
    role_exists="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname='${bundle_db_user}'" 2>/dev/null || true)"
    if [[ "${role_exists}" != "1" ]]; then
      err "Bundle POSTGRES_USER '${bundle_db_user}' does not exist in this postgres volume (volume role: '${volume_db_user}')."
      err "Nothing was changed. Re-install with a matching POSTGRES_USER (or create the role manually), then retry."
      err "کاربر دیتابیس داخل باندل با این سرور همخوانی ندارد؛ هیچ تغییری اعمال نشد. ابتدا نصب را با همان POSTGRES_USER انجام دهید و دوباره تلاش کنید."
      exit 1
    fi
  fi

  # If the target DB is populated, refuse without explicit OVERWRITE.
  local row_count
  row_count="$(docker exec "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d "${bundle_db_name}" -tAc \
      "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
  if [[ "${row_count:-0}" -gt 0 ]]; then
    warn "Target DB already has ${row_count} table(s). Restore will WIPE them."
    read -r -p "Type OVERWRITE to continue (anything else aborts): " confirm
    if [[ "${confirm}" != "OVERWRITE" ]]; then
      warn "Aborted by operator. Nothing was changed (.env and DB both intact)."
      exit 1
    fi
  fi

  # ── Postgres restore ──
  # api/bot/worker keep idle SQLAlchemy pool connections open, which makes
  # DROP DATABASE fail with "database is being accessed by other users"
  # and abort the whole script. Stop them first (the next-steps deploy
  # starts them again), then terminate any straggler sessions — the same
  # statement restore.sh uses.
  info "Stopping app containers (api/bot/worker) holding DB connections…"
  docker stop telegramsellbot-api telegramsellbot-bot telegramsellbot-worker >/dev/null 2>&1 || true

  info "Terminating remaining connections to ${bundle_db_name}…"
  docker exec "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d postgres \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${bundle_db_name}';" \
      >/dev/null 2>&1 || true

  info "Restoring PostgreSQL…"
  docker exec "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d postgres -c "DROP DATABASE IF EXISTS \"${bundle_db_name}\";" >/dev/null
  docker exec "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d postgres -c "CREATE DATABASE \"${bundle_db_name}\" OWNER \"${bundle_db_user}\";" >/dev/null
  gunzip -c "${stage}/db.sql.gz" | docker exec -i "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d "${bundle_db_name}" >/dev/null
  ok "PostgreSQL restored"

  # ── .env handling — deliberately AFTER the DB restore, so any failure
  #    or abort above leaves the host with its own working .env. ──
  if [[ -f "${ENV_FILE}" ]]; then
    warn "An existing .env is already present at ${ENV_FILE}."
    local env_backup="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    cp -p "${ENV_FILE}" "${env_backup}"
    ok "Snapshotted existing .env → ${env_backup}"
  fi
  cp -p "${stage}/.env" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  ok ".env restored (mode 600)"

  # ── Sync the volume's role password to the restored .env ──
  # An initialised postgres volume keeps the password it was created with;
  # the POSTGRES_PASSWORD env var is ignored on later boots. Without this
  # ALTER, api/bot/worker would all fail TCP auth after './deploy.sh full'
  # (and deploy.sh's failure hint would suggest deleting the volume we
  # just restored into).
  if [[ -n "${bundle_db_password}" ]]; then
    info "Syncing postgres role password to the restored .env…"
    local pw_sql
    pw_sql=${bundle_db_password//\'/\'\'}   # double single quotes for the SQL literal
    if ! docker exec -i "${POSTGRES_CONTAINER}" psql -U "${volume_db_user}" -d postgres \
          -q -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE "${bundle_db_user}" WITH PASSWORD '${pw_sql}';
SQL
    then
      err "DB restored, but syncing the role password failed. Fix it manually before deploying:"
      err "  docker exec -it ${POSTGRES_CONTAINER} psql -U ${volume_db_user} -d postgres -c \"ALTER ROLE \\\"${bundle_db_user}\\\" WITH PASSWORD '<POSTGRES_PASSWORD from .env>';\""
      err "دیتابیس بازیابی شد اما همگام‌سازی رمز دیتابیس ناموفق بود؛ پیش از دیپلوی، دستور بالا را اجرا کنید."
      exit 1
    fi
    ok "Role password synced — restored .env now matches the postgres volume"
  else
    warn "Bundle .env has no POSTGRES_PASSWORD — role password left unchanged; deploy may fail to authenticate."
  fi

  # ── ready_configs ──
  if [[ -d "${stage}/ready_configs" ]]; then
    info "Restoring ready_configs/…"
    rm -rf "${READY_CONFIGS_DIR}"
    mv "${stage}/ready_configs" "${READY_CONFIGS_DIR}"
    ok "ready_configs restored"
  fi

  echo
  ok "Migration restore complete."
  echo
  echo -e "${BOLD}Next steps on this server:${NC}"
  echo -e "  1. ${BOLD}./deploy.sh full${NC}   (build images + run schema migrations)"
  echo -e "  2. ${BOLD}./doctor.sh${NC}        (verify everything is green)"
  echo
  echo -e "${DIM}api/bot/worker were stopped for the restore — step 1 starts them again.${NC}"
  echo -e "${DIM}The previous .env (if any) was snapshotted alongside as .env.bak.<ts>.${NC}"
}

# ─────────────────────────────────────────────────────────────────────────
#  main
# ─────────────────────────────────────────────────────────────────────────

if [[ $# -lt 1 ]]; then
  usage
  exit 2
fi

case "$1" in
  create)  shift; cmd_create  "$@" ;;
  restore) shift; cmd_restore "$@" ;;
  -h|--help|help) usage ;;
  *) err "unknown command: $1"; usage; exit 2 ;;
esac
