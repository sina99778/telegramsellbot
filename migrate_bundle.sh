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

  # ── .env handling ──
  if [[ -f "${ENV_FILE}" ]]; then
    warn "An existing .env is already present at ${ENV_FILE}."
    local env_backup="${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    cp -p "${ENV_FILE}" "${env_backup}"
    ok "Snapshotted existing .env → ${env_backup}"
  fi
  cp -p "${stage}/.env" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  ok ".env restored (mode 600)"

  # ── Postgres restore ──
  require_postgres_running

  # If the target DB is populated, refuse without explicit OVERWRITE.
  local row_count
  row_count="$(docker exec "${POSTGRES_CONTAINER}" psql -U "$(read_env_value POSTGRES_USER || echo telegramsellbot)" \
      -d "$(read_env_value POSTGRES_DB || echo telegramsellbot)" -tAc \
      "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'" 2>/dev/null || echo 0)"
  if [[ "${row_count:-0}" -gt 0 ]]; then
    warn "Target DB already has ${row_count} table(s). Restore will WIPE them."
    read -r -p "Type OVERWRITE to continue (anything else aborts): " confirm
    if [[ "${confirm}" != "OVERWRITE" ]]; then
      warn "Aborted by operator. .env was restored; DB left intact."
      exit 1
    fi
  fi

  info "Restoring PostgreSQL…"
  local db_user db_name
  db_user="$(read_env_value POSTGRES_USER)"; db_user="${db_user:-telegramsellbot}"
  db_name="$(read_env_value POSTGRES_DB)";   db_name="${db_name:-telegramsellbot}"
  docker exec "${POSTGRES_CONTAINER}" psql -U "${db_user}" -d postgres -c "DROP DATABASE IF EXISTS \"${db_name}\";" >/dev/null
  docker exec "${POSTGRES_CONTAINER}" psql -U "${db_user}" -d postgres -c "CREATE DATABASE \"${db_name}\";" >/dev/null
  gunzip -c "${stage}/db.sql.gz" | docker exec -i "${POSTGRES_CONTAINER}" psql -U "${db_user}" -d "${db_name}" >/dev/null
  ok "PostgreSQL restored"

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
