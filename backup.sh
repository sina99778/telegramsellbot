#!/bin/bash
# ============================================================================
#  TelegramSellBot — Comprehensive backup
# ----------------------------------------------------------------------------
#  Produces ONE plain-text tar.gz that contains EVERYTHING needed to bring
#  the bot up on a fresh host:
#
#      db.sql.gz                 — gzipped pg_dump of the bot DB
#      env                       — full .env (renamed so it doesn't auto-load)
#      ready_configs/            — ready-config uploads (if dir exists)
#      xui_databases/<srv>.db    — each active X-UI panel's database file
#      manifest.json             — version + timestamp + git sha + hostname
#
#  This is the SAME bundle the install.sh "Migration Bundle" feature
#  produces — but without any encryption layer. The operator asked for
#  plain bundles ("no .enc") so they can drop the file straight into
#  another tool / open it with tar without ceremony.
#
#  Filename: backups/tsb_backup_<UTC-timestamp>.tar.gz
# ============================================================================
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_DIR}"

ENV_FILE="${PROJECT_DIR}/.env"
READY_CONFIGS_DIR="${PROJECT_DIR}/ready_configs"
BACKUPS_DIR="${PROJECT_DIR}/backups"
POSTGRES_CONTAINER="telegramsellbot-postgres"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
DIM='\033[2m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[ OK ]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERR!]${NC} $*" >&2; }

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

# ── Output paths ────────────────────────────────────────────────────────
umask 077
mkdir -p "${BACKUPS_DIR}"
chmod 700 "${BACKUPS_DIR}" 2>/dev/null || true
TS="$(date -u +%Y%m%d_%H%M%S)"
OUT="${BACKUPS_DIR}/tsb_backup_${TS}.tar.gz"

echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}    TelegramSellBot — Comprehensive Backup${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

# Staging directory — wiped on exit.
STAGE="$(mktemp -d -t tsb_backup.XXXXXX)"
trap 'rm -rf "${STAGE}"' EXIT

# ── 1. Postgres dump ────────────────────────────────────────────────────
info "Dumping PostgreSQL database…"
DB_USER="$(read_env_value POSTGRES_USER)"; DB_USER="${DB_USER:-telegramsellbot}"
DB_NAME="$(read_env_value POSTGRES_DB)";   DB_NAME="${DB_NAME:-telegramsellbot}"
docker exec "${POSTGRES_CONTAINER}" pg_dump -U "${DB_USER}" -d "${DB_NAME}" -F p \
  | gzip > "${STAGE}/db.sql.gz"
DB_SIZE="$(du -h "${STAGE}/db.sql.gz" | awk '{print $1}')"
ok "Database dump: ${DB_SIZE}"

# ── 2. .env (renamed to plain "env" so a careless `source` doesn't load it) ──
if [[ -f "${ENV_FILE}" ]]; then
  cp -p "${ENV_FILE}" "${STAGE}/env"
  ok ".env included"
else
  warn ".env not found at ${ENV_FILE} — bundle won't include secrets"
fi

# ── 3. ready_configs/ ───────────────────────────────────────────────────
if [[ -d "${READY_CONFIGS_DIR}" ]]; then
  cp -R "${READY_CONFIGS_DIR}" "${STAGE}/ready_configs"
  COUNT="$(find "${STAGE}/ready_configs" -type f | wc -l | tr -d ' ')"
  ok "ready_configs/ included (${COUNT} files)"
fi

# ── 4. X-UI panel databases ─────────────────────────────────────────────
#
# Best-effort: ask each active X-UI server for its /server/getDb dump.
# A server that's unreachable or has bad credentials is logged but
# DOESN'T abort the whole backup — operators usually have at least one
# panel up and they care more about the bot DB than any single panel.
info "Trying to pull X-UI panel databases (best-effort)…"
mkdir -p "${STAGE}/xui_databases"
XUI_OUT_DIR="${STAGE}/xui_databases"

PYTHON_RUN=( docker compose -f docker-compose.prod.yml exec -T api python )
if ! docker compose version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then
    PYTHON_RUN=( docker-compose -f docker-compose.prod.yml exec -T api python )
  fi
fi

if "${PYTHON_RUN[@]}" - <<'PY' > "${XUI_OUT_DIR}/_xui_pull.log" 2>&1
import asyncio, base64, json, os, pathlib, sys

async def main():
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from core.database import AsyncSessionFactory
    from models.xui import XUIServerRecord
    from services.xui.runtime import create_xui_client_for_server
    from services.xui.client import XUIClientError

    out_dir = pathlib.Path("/tmp/xui_dump")
    out_dir.mkdir(exist_ok=True)
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(
            select(XUIServerRecord)
            .options(selectinload(XUIServerRecord.credentials))
            .where(XUIServerRecord.is_active.is_(True), XUIServerRecord.health_status != "deleted")
        )).scalars().all()
        report = {"servers": []}
        for s in rows:
            if s.credentials is None:
                report["servers"].append({"name": s.name, "ok": False, "error": "no credentials"})
                continue
            try:
                async with create_xui_client_for_server(s) as client:
                    blob = await client.get_db_backup()
                safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in s.name)[:50]
                (out_dir / f"{safe}.db").write_bytes(blob)
                report["servers"].append({"name": s.name, "ok": True, "bytes": len(blob)})
            except Exception as exc:
                report["servers"].append({"name": s.name, "ok": False, "error": str(exc)[:300]})
        print("REPORT_JSON_BEGIN")
        print(json.dumps(report))
        print("REPORT_JSON_END")

asyncio.run(main())
PY
then
  : # ran OK; pull files via docker cp
  CONTAINER_ID="$(docker compose -f docker-compose.prod.yml ps -q api 2>/dev/null || docker-compose -f docker-compose.prod.yml ps -q api 2>/dev/null || true)"
  if [[ -n "${CONTAINER_ID}" ]]; then
    # Copy out anything the python helper wrote, ignoring errors quietly.
    docker cp "${CONTAINER_ID}:/tmp/xui_dump/." "${XUI_OUT_DIR}/" 2>/dev/null || true
    # Tidy up the temp dir inside the container so subsequent runs start fresh.
    docker exec "${CONTAINER_ID}" rm -rf /tmp/xui_dump 2>/dev/null || true
  fi
  COUNT="$(find "${XUI_OUT_DIR}" -maxdepth 1 -type f -name '*.db' 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "${COUNT}" -gt 0 ]]; then
    ok "X-UI panel dumps: ${COUNT} file(s)"
  else
    warn "No X-UI panel databases could be pulled (this is fine if you have no active panels yet)"
  fi
else
  warn "Could not invoke the api container — X-UI panel dumps skipped."
  warn "Bot DB + .env are still in the bundle, so it's still a complete bot backup."
fi

# ── 5. Manifest ─────────────────────────────────────────────────────────
SHA="$(git -C "${PROJECT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
BRANCH="$(git -C "${PROJECT_DIR}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
HAVE_ENV="$( [[ -f "${STAGE}/env" ]] && echo true || echo false )"
HAVE_READY="$( [[ -d "${STAGE}/ready_configs" ]] && echo true || echo false )"
XUI_COUNT="$(find "${STAGE}/xui_databases" -maxdepth 1 -type f -name '*.db' 2>/dev/null | wc -l | tr -d ' ')"

cat > "${STAGE}/manifest.json" <<EOF
{
  "format_version": 2,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "hostname": "$(hostname)",
  "git_branch": "${BRANCH}",
  "git_sha": "${SHA}",
  "encrypted": false,
  "contents": {
    "db_dump": true,
    "env": ${HAVE_ENV},
    "ready_configs": ${HAVE_READY},
    "xui_databases_count": ${XUI_COUNT}
  }
}
EOF

# ── 6. Tar it up ────────────────────────────────────────────────────────
info "Bundling into ${OUT}…"
( cd "${STAGE}" && tar -czf "${OUT}" \
    db.sql.gz manifest.json \
    $( [[ -f env ]] && echo env || true ) \
    $( [[ -d ready_configs ]] && echo ready_configs || true ) \
    $( [[ -d xui_databases ]] && echo xui_databases || true ) \
)
chmod 600 "${OUT}"

SIZE="$(du -h "${OUT}" | awk '{print $1}')"
echo
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
ok "Backup complete!"
echo -e "  File:  ${BOLD}${OUT}${NC}"
echo -e "  Size:  ${BOLD}${SIZE}${NC}"
echo
echo -e "${DIM}To restore on this OR a new host:${NC}"
echo -e "  ${BOLD}./restore.sh ${OUT}${NC}"
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
