#!/usr/bin/env bash
# ============================================================================
#  TelegramSellBot Installer & Operations Console
# ----------------------------------------------------------------------------
#  Single entry-point for everything an operator does on a production VPS:
#    * configure .env
#    * install Docker / Nginx / SSL
#    * pull the latest code AND deploy it in one action
#    * restart, view logs, inspect status
#    * back up / restore the database
#    * uninstall
#
#  Run as root (the script will re-exec via sudo if needed).
# ============================================================================
set -Eeuo pipefail

INSTALLER_VERSION="2026-05-24-2"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
SETUP_SCRIPT="${PROJECT_DIR}/setup.sh"
DEPLOY_SCRIPT="${PROJECT_DIR}/deploy.sh"
BACKUP_SCRIPT="${PROJECT_DIR}/backup.sh"
RESTORE_SCRIPT="${PROJECT_DIR}/restore.sh"
DOCTOR_SCRIPT="${PROJECT_DIR}/doctor.sh"
NGINX_SITE_PATH="/etc/nginx/sites-available/telegramsellbot.conf"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/telegramsellbot.conf"
LOG_FILE="${PROJECT_DIR}/installer.log"

# Containers we own — used for status checks and uninstall cleanup.
CONTAINERS=(
  telegramsellbot-api
  telegramsellbot-bot
  telegramsellbot-worker
  telegramsellbot-postgres
  telegramsellbot-redis
)

# ── colours / formatting ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
GRAY='\033[0;90m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ── logging helpers ─────────────────────────────────────────────────────────
log_to_file() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "${LOG_FILE}" 2>/dev/null || true
}

info()    { echo -e "${BLUE}[INFO]${NC} $*";    log_to_file "INFO  $*"; }
success() { echo -e "${GREEN}[ OK ]${NC} $*";   log_to_file "OK    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*";  log_to_file "WARN  $*"; }
error()   { echo -e "${RED}[ERR!]${NC} $*" >&2; log_to_file "ERROR $*"; }

pause() {
  echo
  read -r -p "$(echo -e "${DIM}Press Enter to return to the menu...${NC}")" _
}

confirm() {
  # confirm "prompt" "EXPECTED_WORD"  → returns 0 if user types the word, 1 otherwise.
  local prompt_text="$1" expected="${2:-yes}" answer=""
  read -r -p "$(echo -e "${YELLOW}${prompt_text}${NC} ")" answer
  [[ "${answer}" == "${expected}" ]]
}

# ── compose helpers ─────────────────────────────────────────────────────────
compose_impl() {
  if docker compose version >/dev/null 2>&1; then echo "plugin"
  elif command -v docker-compose >/dev/null 2>&1; then echo "legacy"
  else echo "missing"
  fi
}

run_compose() {
  local impl
  impl="$(compose_impl)"
  case "${impl}" in
    plugin) docker compose -f "${COMPOSE_FILE}" "$@" ;;
    legacy) docker-compose -f "${COMPOSE_FILE}" "$@" ;;
    *) error "Docker Compose is not installed."; return 1 ;;
  esac
}

# ── env file helpers ────────────────────────────────────────────────────────
read_env_value() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  local line
  line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
  [[ -n "${line}" ]] || return 0
  printf '%s' "${line#*=}"
}

backup_env() {
  if [[ -f "${ENV_FILE}" ]]; then
    cp -p "${ENV_FILE}" "${ENV_FILE}.bak.$(date +%Y%m%d_%H%M%S)"
    success "Existing .env snapshotted as a sibling .bak file."
  fi
}

# ── system requirement checks ───────────────────────────────────────────────
require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      exec sudo -E bash "$0" "$@"
    fi
    error "This installer requires root privileges and sudo is not installed."
    exit 1
  fi
}

require_commands() {
  local cmd missing=0
  for cmd in "$@"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      error "Missing required command: ${cmd}"
      missing=1
    fi
  done
  [[ "${missing}" -eq 0 ]] || return 1
}

# ── header / status display ─────────────────────────────────────────────────
git_short_sha() {
  if [[ -d "${PROJECT_DIR}/.git" ]] && command -v git >/dev/null 2>&1; then
    (cd "${PROJECT_DIR}" && git rev-parse --short HEAD 2>/dev/null || echo "unknown")
  else
    echo "n/a"
  fi
}

git_branch_name() {
  if [[ -d "${PROJECT_DIR}/.git" ]] && command -v git >/dev/null 2>&1; then
    (cd "${PROJECT_DIR}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")
  else
    echo "n/a"
  fi
}

container_status_dot() {
  # green dot if running, yellow if exists-but-not-running, gray if missing.
  local name="$1" status=""
  status="$(docker inspect -f '{{.State.Status}}' "${name}" 2>/dev/null || echo "missing")"
  case "${status}" in
    running) echo -e "${GREEN}●${NC}" ;;
    missing) echo -e "${GRAY}○${NC}" ;;
    *)       echo -e "${YELLOW}◐${NC}" ;;
  esac
}

print_header() {
  clear
  local sha branch env_status compose_status
  sha="$(git_short_sha)"
  branch="$(git_branch_name)"
  env_status="$([[ -f "${ENV_FILE}" ]] && echo -e "${GREEN}present${NC}" || echo -e "${RED}missing${NC}")"
  case "$(compose_impl)" in
    plugin) compose_status="${GREEN}docker compose (plugin)${NC}" ;;
    legacy) compose_status="${YELLOW}docker-compose (legacy)${NC}" ;;
    *)      compose_status="${RED}not installed${NC}" ;;
  esac

  echo -e "${BOLD}${CYAN}╔══════════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BOLD}${CYAN}║${NC}        ${BOLD}TelegramSellBot — Installer & Ops Console${NC}                ${BOLD}${CYAN}║${NC}"
  echo -e "${BOLD}${CYAN}╚══════════════════════════════════════════════════════════════════╝${NC}"
  echo -e "  ${DIM}installer ${INSTALLER_VERSION}  •  ${PROJECT_DIR}${NC}"
  echo
  printf "  %-12s %s\n"  "branch:"   "${branch} @ ${sha}"
  printf "  %-12s "      "compose:"
  echo -e "${compose_status}"
  printf "  %-12s "      ".env:"
  echo -e "${env_status}"
  echo
  printf "  services:  "
  echo -e "$(container_status_dot telegramsellbot-api) api   $(container_status_dot telegramsellbot-bot) bot   $(container_status_dot telegramsellbot-worker) worker   $(container_status_dot telegramsellbot-postgres) postgres   $(container_status_dot telegramsellbot-redis) redis"
  echo -e "  ${DIM}● running   ◐ stopped   ○ missing${NC}"
  echo
}

# ============================================================================
#  ACTION: .env builder
# ============================================================================
prompt_with_default() {
  local prompt_text="$1" var_name="$2" default_value="${3:-}" is_secret="${4:-0}" value=""

  if [[ -n "${default_value}" ]]; then
    if [[ "${is_secret}" == "1" ]]; then
      read -r -s -p "${prompt_text} [press Enter to keep current]: " value; echo
    else
      read -r -p "${prompt_text} [${default_value}]: " value
    fi
    [[ -n "${value}" ]] || value="${default_value}"
  else
    while [[ -z "${value}" ]]; do
      if [[ "${is_secret}" == "1" ]]; then
        read -r -s -p "${prompt_text}: " value; echo
      else
        read -r -p "${prompt_text}: " value
      fi
      [[ -n "${value}" ]] || warn "This value is required."
    done
  fi
  printf -v "${var_name}" '%s' "${value}"
}

extract_domain_from_url() {
  local url="$1"
  url="${url#http://}"; url="${url#https://}"
  printf '%s' "${url%%/*}"
}

generate_fernet_key() {
  if ! python3 -c "from cryptography.fernet import Fernet" 2>/dev/null; then
    info "Installing cryptography for Fernet key generation..."
    pip3 install -q cryptography 2>/dev/null \
      || { apt-get install -y -qq python3-pip >/dev/null && pip3 install -q cryptography; }
  fi
  python3 - <<'PY'
from cryptography.fernet import Fernet
print(Fernet.generate_key().decode())
PY
}

generate_password() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
}

setup_env_builder() {
  print_header
  echo -e "${BOLD}⚙️  Configure .env${NC}"
  echo -e "${DIM}Existing values are preserved if you press Enter at the prompt.${NC}"
  echo
  require_commands python3 || { pause; return; }

  local current_bot_token current_owner current_xui_base_url current_xui_username current_xui_password
  local current_nowpayments_api_key current_domain current_app_secret current_postgres_password
  local current_redis_password current_admin_api_key current_nowpayments_ipn_secret current_tetrapay_api_key
  local bot_token owner_telegram_id xui_base_url xui_username xui_password nowpayments_api_key domain_name
  local app_secret_key postgres_password redis_password admin_api_key nowpayments_ipn_secret tetrapay_api_key
  local postgres_user postgres_db database_url redis_url webhook_url support_url web_base_url tetrapay_callback_url

  current_bot_token="$(read_env_value BOT_TOKEN)"
  current_owner="$(read_env_value OWNER_TELEGRAM_ID)"
  current_xui_base_url="$(read_env_value XUI_BASE_URL)"
  current_xui_username="$(read_env_value XUI_USERNAME)"
  current_xui_password="$(read_env_value XUI_PASSWORD)"
  current_nowpayments_api_key="$(read_env_value NOWPAYMENTS_API_KEY)"
  current_domain="$(extract_domain_from_url "$(read_env_value WEB_BASE_URL)")"
  current_app_secret="$(read_env_value APP_SECRET_KEY)"
  current_postgres_password="$(read_env_value POSTGRES_PASSWORD)"
  current_redis_password="$(read_env_value REDIS_PASSWORD)"
  current_admin_api_key="$(read_env_value ADMIN_API_KEY)"
  current_nowpayments_ipn_secret="$(read_env_value NOWPAYMENTS_IPN_SECRET)"
  current_tetrapay_api_key="$(read_env_value TETRAPAY_API_KEY)"

  prompt_with_default "Bot Token" bot_token "${current_bot_token}" 1
  prompt_with_default "Admin Telegram ID" owner_telegram_id "${current_owner}"
  prompt_with_default "X-UI Base URL (e.g. http://ip:2053)" xui_base_url "${current_xui_base_url}"
  prompt_with_default "X-UI Username" xui_username "${current_xui_username}"
  prompt_with_default "X-UI Password" xui_password "${current_xui_password}" 1
  prompt_with_default "NOWPayments API Key" nowpayments_api_key "${current_nowpayments_api_key}" 1
  prompt_with_default "TetraPay API Key (leave empty to skip)" tetrapay_api_key "${current_tetrapay_api_key}" 1
  prompt_with_default "Domain Name (for webhook/API)" domain_name "${current_domain}"

  app_secret_key="${current_app_secret:-$(generate_fernet_key)}"
  postgres_password="${current_postgres_password:-$(generate_password)}"
  redis_password="${current_redis_password:-$(generate_password)}"
  admin_api_key="${current_admin_api_key:-$(generate_password)}"
  nowpayments_ipn_secret="${current_nowpayments_ipn_secret:-$(generate_password)}"

  postgres_user="telegramsellbot"
  postgres_db="telegramsellbot"
  database_url="postgresql+asyncpg://${postgres_user}:${postgres_password}@postgres:5432/${postgres_db}"
  redis_url="redis://default:${redis_password}@redis:6379/0"
  webhook_url="https://${domain_name}/api/webhooks/nowpayments"
  tetrapay_callback_url="https://${domain_name}/api/webhooks/tetrapay"
  support_url="https://${domain_name}"
  web_base_url="https://${domain_name}"

  backup_env

  cat > "${ENV_FILE}" <<EOF
APP_ENV=production
APP_DEBUG=false
LOG_LEVEL=INFO
APP_SECRET_KEY=${app_secret_key}

BOT_TOKEN=${bot_token}
BOT_PARSE_MODE=HTML
BOT_DROP_PENDING_UPDATES=false
OWNER_TELEGRAM_ID=${owner_telegram_id}
ADMIN_API_KEY=${admin_api_key}

POSTGRES_DB=${postgres_db}
POSTGRES_USER=${postgres_user}
POSTGRES_PASSWORD=${postgres_password}
DATABASE_URL=${database_url}

REDIS_PASSWORD=${redis_password}
REDIS_URL=${redis_url}

XUI_BASE_URL=${xui_base_url}
XUI_USERNAME=${xui_username}
XUI_PASSWORD=${xui_password}

NOWPAYMENTS_API_KEY=${nowpayments_api_key}
NOWPAYMENTS_BASE_URL=https://api.nowpayments.io/v1
NOWPAYMENTS_IPN_SECRET=${nowpayments_ipn_secret}
NOWPAYMENTS_IPN_CALLBACK_URL=${webhook_url}

TETRAPAY_API_KEY=${tetrapay_api_key:-CHANGE_ME}
TETRAPAY_BASE_URL=https://tetra98.com/api
TETRAPAY_CALLBACK_URL=${tetrapay_callback_url}

WEB_BASE_URL=${web_base_url}
SUPPORT_URL=${support_url}
EOF

  chmod 600 "${ENV_FILE}"
  success ".env updated at ${ENV_FILE} (mode 600)."
  warn "Auto-generated secrets were preserved unless you typed new values."
  pause
}

# ============================================================================
#  ACTION: install prerequisites + SSL
# ============================================================================
install_prerequisites_and_ssl() {
  print_header
  echo -e "${BOLD}🔐 Install Prerequisites & SSL${NC}"
  echo -e "${DIM}Installs Docker, Compose, Nginx and a Let's Encrypt cert.${NC}"
  echo

  export DEBIAN_FRONTEND=noninteractive
  info "Updating apt index..."
  apt-get update
  info "Installing base packages..."
  apt-get install -y ca-certificates curl gnupg lsb-release nginx certbot python3-certbot-nginx

  if ! command -v docker >/dev/null 2>&1; then
    info "Installing docker.io..."
    apt-get install -y docker.io
  fi

  if ! docker compose version >/dev/null 2>&1; then
    info "Installing docker-compose-plugin..."
    apt-get install -y docker-compose-plugin || true
  fi
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    apt-get install -y docker-compose || true
  fi

  case "$(compose_impl)" in
    plugin) success "Modern docker compose plugin available." ;;
    legacy) warn  "Only legacy docker-compose available; deploys will use the compat path." ;;
    *)      error "Could not install any Docker Compose."; pause; return ;;
  esac

  systemctl enable --now docker
  systemctl enable --now nginx

  local current_domain current_email="" email domain_name
  current_domain="$(extract_domain_from_url "$(read_env_value WEB_BASE_URL)")"

  prompt_with_default "Email for Let's Encrypt" email "${current_email}"
  prompt_with_default "Domain pointing to this server" domain_name "${current_domain}"

  cat > "${NGINX_SITE_PATH}" <<EOF
server {
    listen 80;
    server_name ${domain_name};

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF

  ln -sf "${NGINX_SITE_PATH}" "${NGINX_SITE_LINK}"
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx

  if ! certbot --nginx --non-interactive --redirect --agree-tos -m "${email}" -d "${domain_name}"; then
    error "Certbot failed. Make sure the domain points to this VPS and ports 80/443 are open."
    pause
    return
  fi

  success "Prerequisites installed and SSL configured for ${domain_name}."
  pause
}

# ============================================================================
#  ACTION: Update & Deploy  (THE big one — pulls latest + rebuilds)
# ============================================================================
fetch_latest_code() {
  # Returns 0 if code was successfully updated (or already current).
  # Returns 1 on hard failure.
  if [[ ! -d "${PROJECT_DIR}/.git" ]]; then
    warn "No .git directory found — falling back to setup.sh snapshot sync."
    [[ -x "${SETUP_SCRIPT}" ]] || chmod +x "${SETUP_SCRIPT}" 2>/dev/null || true
    if [[ -x "${SETUP_SCRIPT}" ]]; then
      (cd /root && "${SETUP_SCRIPT}" sync-only)
      return $?
    fi
    error "setup.sh is missing too — cannot fetch new code."
    return 1
  fi

  require_commands git || return 1

  info "Fetching latest code from origin..."
  if ! (cd "${PROJECT_DIR}" && git fetch --prune origin); then
    error "git fetch failed."
    return 1
  fi

  local current_branch remote_branch
  current_branch="$(cd "${PROJECT_DIR}" && git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [[ -z "${current_branch}" || "${current_branch}" == "HEAD" ]]; then
    current_branch="master"
    info "Detached HEAD detected; resetting to origin/master."
    (cd "${PROJECT_DIR}" && git checkout -B master origin/master) || {
      error "Could not check out master."; return 1; }
  fi
  remote_branch="origin/${current_branch}"

  # Refuse to clobber operator-side changes silently.
  local dirty_count
  dirty_count="$(cd "${PROJECT_DIR}" && git status --porcelain | wc -l | tr -d ' ')"
  if [[ "${dirty_count}" -gt 0 ]]; then
    warn "There are ${dirty_count} uncommitted local changes in ${PROJECT_DIR}."
    echo -e "${YELLOW}Update can only proceed by discarding them.${NC}"
    if ! confirm "Type DISCARD to wipe local changes and continue, anything else to abort:" "DISCARD"; then
      warn "Update aborted — your local changes are preserved."
      return 1
    fi
    info "Discarding local changes..."
    (cd "${PROJECT_DIR}" && git reset --hard HEAD && git clean -fd)
  fi

  info "Resetting ${current_branch} to ${remote_branch}..."
  if ! (cd "${PROJECT_DIR}" && git reset --hard "${remote_branch}"); then
    error "git reset --hard ${remote_branch} failed."
    return 1
  fi

  chmod +x "${SETUP_SCRIPT}" "${DEPLOY_SCRIPT}" "${BACKUP_SCRIPT}" "${RESTORE_SCRIPT}" "${DOCTOR_SCRIPT}" 2>/dev/null || true

  local new_sha new_branch
  new_sha="$(git_short_sha)"
  new_branch="$(git_branch_name)"
  success "Code is now at ${new_branch} @ ${new_sha}."
  return 0
}

update_and_deploy() {
  print_header
  echo -e "${BOLD}⚡ Update & Deploy${NC}"
  echo -e "${DIM}Pulls the latest code from GitHub, then runs a full rebuild.${NC}"
  echo

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found. Configure it first (option 4)."
    pause
    return
  fi

  if [[ "$(compose_impl)" == "missing" ]]; then
    error "Docker Compose is not installed. Run option 5 first."
    pause
    return
  fi

  local before_sha after_sha
  before_sha="$(git_short_sha)"

  if ! fetch_latest_code; then
    error "Code update failed — aborting deploy to avoid running an inconsistent state."
    pause
    return
  fi

  after_sha="$(git_short_sha)"

  if [[ "${before_sha}" == "${after_sha}" && "${before_sha}" != "n/a" && "${before_sha}" != "unknown" ]]; then
    info "Code was already at ${after_sha}; redeploying anyway to apply env or image changes."
  else
    info "Code moved from ${before_sha} → ${after_sha}; deploying..."
  fi

  chmod +x "${DEPLOY_SCRIPT}"
  if (cd "${PROJECT_DIR}" && "${DEPLOY_SCRIPT}" full); then
    success "Update & deploy completed successfully."
  else
    error "Deploy step failed. Inspect the output above and try option 6 → All to see container logs."
  fi
  pause
}

full_deploy() {
  print_header
  echo -e "${BOLD}🚀 Full Deploy (no pull)${NC}"
  echo -e "${DIM}Rebuilds & migrates with the code already on disk. Skips git.${NC}"
  echo

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found. Configure it first (option 4)."; pause; return
  fi
  if [[ "$(compose_impl)" == "missing" ]]; then
    error "Docker Compose is not installed. Run option 5 first."; pause; return
  fi

  chmod +x "${DEPLOY_SCRIPT}"
  if (cd "${PROJECT_DIR}" && "${DEPLOY_SCRIPT}" full); then
    success "Full deploy completed."
  else
    error "Deploy failed."
  fi
  pause
}

quick_reload() {
  print_header
  echo -e "${BOLD}🔄 Quick Reload${NC}"
  echo -e "${DIM}Restarts api / bot / worker without rebuilding images.${NC}"
  echo

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found."; pause; return
  fi
  if [[ "$(compose_impl)" == "missing" ]]; then
    error "Docker Compose is not installed."; pause; return
  fi

  chmod +x "${DEPLOY_SCRIPT}"
  if (cd "${PROJECT_DIR}" && "${DEPLOY_SCRIPT}" reload); then
    success "Quick reload completed."
  else
    error "Quick reload failed."
  fi
  pause
}

# ============================================================================
#  ACTION: Doctor — self-diagnose + self-heal
# ============================================================================
doctor_run() {
  print_header
  echo -e "${BOLD}🩺 Doctor — Self-Diagnose & Self-Heal${NC}"
  echo -e "${DIM}Walks through ~15 checks (containers, DB schema, heartbeats, logs)"
  echo -e "and tries to auto-fix what it can. Choose a mode below.${NC}"
  echo
  echo "  1) Auto-fix mode    (default — find AND fix what's broken)"
  echo "  2) Dry-run scan     (only diagnose; never modify anything)"
  echo "  0) Back"
  echo
  read -r -p "Choose: " mode
  case "${mode}" in
    1|"") ;;
    2) DOCTOR_DRYRUN=1 ;;
    0) return ;;
    *) warn "Invalid option."; pause; return ;;
  esac

  if [[ ! -x "${DOCTOR_SCRIPT}" ]]; then
    chmod +x "${DOCTOR_SCRIPT}" 2>/dev/null || true
  fi
  if [[ ! -f "${DOCTOR_SCRIPT}" ]]; then
    error "doctor.sh not found at ${DOCTOR_SCRIPT}. Pull the latest code (option 1) and try again."
    pause
    return
  fi

  echo
  if [[ "${DOCTOR_DRYRUN:-0}" -eq 1 ]]; then
    (cd "${PROJECT_DIR}" && "${DOCTOR_SCRIPT}" --dry-run) || true
  else
    (cd "${PROJECT_DIR}" && "${DOCTOR_SCRIPT}") || true
  fi
  unset DOCTOR_DRYRUN
  pause
}


# ============================================================================
#  ACTION: logs submenu
# ============================================================================
view_logs_for() {
  local service="$1" lines="${2:-200}"
  echo
  info "Showing last ${lines} lines for ${service}. Press Ctrl-C to exit follow mode."
  echo
  if [[ "${service}" == "all" ]]; then
    run_compose logs --tail="${lines}" -f api bot worker postgres redis || true
  else
    run_compose logs --tail="${lines}" -f "${service}" || true
  fi
}

logs_menu() {
  while true; do
    print_header
    echo -e "${BOLD}📜 Service Logs${NC}"
    echo
    echo "  1) Bot        (live, tail 200)"
    echo "  2) API        (live, tail 200)"
    echo "  3) Worker     (live, tail 200)"
    echo "  4) Postgres   (live, tail 200)"
    echo "  5) Redis      (live, tail 200)"
    echo "  6) All        (combined, tail 100)"
    echo "  7) Bot — tail 1000 (no follow)"
    echo "  8) API — tail 1000 (no follow)"
    echo "  0) Back"
    echo
    read -r -p "Choose: " ch
    case "${ch}" in
      1) view_logs_for bot 200 ;;
      2) view_logs_for api 200 ;;
      3) view_logs_for worker 200 ;;
      4) view_logs_for postgres 200 ;;
      5) view_logs_for redis 200 ;;
      6) view_logs_for all 100 ;;
      7) run_compose logs --tail=1000 bot || true; pause ;;
      8) run_compose logs --tail=1000 api || true; pause ;;
      0) return ;;
      *) warn "Invalid option."; pause ;;
    esac
  done
}

# ============================================================================
#  ACTION: service status
# ============================================================================
service_status() {
  print_header
  echo -e "${BOLD}📊 Service Status${NC}"
  echo

  if ! command -v docker >/dev/null 2>&1; then
    error "Docker is not installed."; pause; return
  fi

  echo -e "${BOLD}Containers${NC}"
  docker ps -a \
    --filter "name=telegramsellbot-" \
    --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" \
    | sed -e "s/Up [^,]*/$(printf '\033[0;32m')&$(printf '\033[0m')/" \
          -e "s/Exited.*$/$(printf '\033[0;31m')&$(printf '\033[0m')/"

  echo
  echo -e "${BOLD}Volumes${NC}"
  docker volume ls --filter "name=telegramsellbot" --format "  {{.Name}}" || true

  echo
  echo -e "${BOLD}Disk usage (top 5 logs)${NC}"
  for c in "${CONTAINERS[@]}"; do
    if docker inspect "${c}" >/dev/null 2>&1; then
      local log_path size=""
      log_path="$(docker inspect -f '{{.LogPath}}' "${c}" 2>/dev/null || true)"
      if [[ -n "${log_path}" && -f "${log_path}" ]]; then
        size="$(du -h "${log_path}" 2>/dev/null | awk '{print $1}')"
        printf "  %-30s %s\n" "${c}" "${size:-?}"
      fi
    fi
  done

  pause
}

# ============================================================================
#  ACTION: backup / restore
# ============================================================================
backup_run() {
  print_header
  echo -e "${BOLD}💾 Create Database Backup${NC}"
  echo
  if [[ ! -x "${BACKUP_SCRIPT}" ]]; then
    chmod +x "${BACKUP_SCRIPT}" 2>/dev/null || true
  fi
  if [[ ! -f "${BACKUP_SCRIPT}" ]]; then
    error "backup.sh not found in ${PROJECT_DIR}."; pause; return
  fi
  (cd "${PROJECT_DIR}" && bash "${BACKUP_SCRIPT}")
  pause
}

list_backups() {
  local dir="${PROJECT_DIR}/backups"
  if [[ ! -d "${dir}" ]]; then
    warn "No backups directory yet."
    return 1
  fi
  local files=("${dir}"/*.sql.gz)
  if [[ ! -e "${files[0]}" ]]; then
    warn "No .sql.gz backups found in ${dir}."
    return 1
  fi
  echo -e "${BOLD}Available backups${NC}"
  local i=1
  for f in "${files[@]}"; do
    printf "  %2d) %s  (%s)\n" "${i}" "$(basename "${f}")" "$(du -h "${f}" | awk '{print $1}')"
    i=$((i + 1))
  done
  return 0
}

restore_run() {
  print_header
  echo -e "${BOLD}🔁 Restore Database from Backup${NC}"
  echo -e "${YELLOW}This will WIPE the current database and replace it with the chosen backup.${NC}"
  echo

  if ! list_backups; then pause; return; fi

  local dir="${PROJECT_DIR}/backups"
  local files=("${dir}"/*.sql.gz)
  echo
  read -r -p "Pick a backup number (or 0 to cancel): " pick
  if ! [[ "${pick}" =~ ^[0-9]+$ ]] || [[ "${pick}" -lt 1 || "${pick}" -gt "${#files[@]}" ]]; then
    warn "Cancelled."; pause; return
  fi
  local chosen="${files[$((pick - 1))]}"
  echo
  warn "About to restore: $(basename "${chosen}")"
  if ! confirm "Type RESTORE to confirm, anything else to cancel:" "RESTORE"; then
    warn "Restore cancelled."; pause; return
  fi
  chmod +x "${RESTORE_SCRIPT}" 2>/dev/null || true
  (cd "${PROJECT_DIR}" && bash "${RESTORE_SCRIPT}" "${chosen}")
  pause
}

backup_menu() {
  while true; do
    print_header
    echo -e "${BOLD}💾 Backup / Restore${NC}"
    echo
    echo "  1) Create New Backup"
    echo "  2) Restore From Backup"
    echo "  3) List Backups"
    echo "  0) Back"
    echo
    read -r -p "Choose: " ch
    case "${ch}" in
      1) backup_run ;;
      2) restore_run ;;
      3) print_header; list_backups || true; pause ;;
      0) return ;;
      *) warn "Invalid option."; pause ;;
    esac
  done
}

# ============================================================================
#  ACTION: database tools
# ============================================================================
wait_for_postgres() {
  local db_user db_name attempt max_attempts
  db_user="$(read_env_value POSTGRES_USER)"; db_user="${db_user:-telegramsellbot}"
  db_name="$(read_env_value POSTGRES_DB)";   db_name="${db_name:-telegramsellbot}"
  max_attempts=30
  for attempt in $(seq 1 "${max_attempts}"); do
    if docker exec telegramsellbot-postgres pg_isready -U "${db_user}" -d "${db_name}" >/dev/null 2>&1; then
      success "PostgreSQL is ready."
      return 0
    fi
    info "Waiting for PostgreSQL (${attempt}/${max_attempts})..."
    sleep 2
  done
  error "PostgreSQL did not become ready in time."
  return 1
}

db_status() {
  print_header
  echo -e "${BOLD}DB Status${NC}"
  echo
  docker ps -a --format "table {{.Names}}\t{{.Status}}" | grep telegramsellbot || true
  echo
  docker volume ls --format "table {{.Name}}" | grep telegramsellbot || true
  pause
}

db_bootstrap_schema() {
  print_header
  echo -e "${BOLD}Bootstrap Schema${NC}"
  echo
  info "Rebuilding API image to ensure bootstrap uses the latest code..."
  run_compose build api || { error "Build failed."; pause; return; }
  run_compose run --rm api python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())" \
    || { error "Bootstrap failed."; pause; return; }
  success "Schema bootstrap completed."
  pause
}

db_restart_postgres() {
  print_header
  echo -e "${BOLD}Restart PostgreSQL${NC}"
  echo
  run_compose restart postgres && success "PostgreSQL restarted." || error "Restart failed."
  pause
}

db_reset_database() {
  print_header
  echo -e "${BOLD}${RED}DESTRUCTIVE: Reset Database${NC}"
  echo -e "${YELLOW}This will DELETE all PostgreSQL data for TelegramSellBot.${NC}"
  echo -e "${YELLOW}Users, orders, wallets, tickets, broadcasts — everything gone.${NC}"
  echo
  if ! confirm "Type RESET to continue, anything else to cancel:" "RESET"; then
    warn "Database reset cancelled."; pause; return
  fi

  run_compose down -v --remove-orphans || true
  docker volume rm telegramsellbot_postgres_data >/dev/null 2>&1 || true
  success "Database volume removed."

  info "Recreating postgres and redis..."
  run_compose up -d postgres redis
  wait_for_postgres || { pause; return; }

  info "Rebuilding API image to bootstrap latest schema..."
  run_compose build api
  run_compose run --rm api python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())"
  success "Database reset and bootstrap completed."
  pause
}

database_tools_menu() {
  while true; do
    print_header
    echo -e "${BOLD}🗄️  Database Tools${NC}"
    echo
    echo "  1) Show DB Status"
    echo "  2) Bootstrap Schema"
    echo "  3) Restart PostgreSQL"
    echo "  4) Reset Database  (DESTRUCTIVE)"
    echo "  0) Back"
    echo
    read -r -p "Choose: " ch
    case "${ch}" in
      1) db_status ;;
      2) db_bootstrap_schema ;;
      3) db_restart_postgres ;;
      4) db_reset_database ;;
      0) return ;;
      *) warn "Invalid option."; pause ;;
    esac
  done
}

# ============================================================================
#  ACTION: uninstall
# ============================================================================
full_uninstall() {
  print_header
  echo -e "${BOLD}${RED}🗑️  Full Uninstall${NC}"
  echo
  echo -e "${YELLOW}This will stop the stack, remove all containers and volumes,"
  echo -e "delete the project directory, and remove the Nginx site config."
  echo -e "Let's Encrypt certificate files are NOT deleted automatically.${NC}"
  echo
  if ! confirm "Type DELETE to continue, anything else to cancel:" "DELETE"; then
    warn "Uninstall cancelled."; pause; return
  fi

  if [[ "$(compose_impl)" != "missing" && -f "${COMPOSE_FILE}" ]]; then
    run_compose down -v --remove-orphans || true
  fi
  docker rm -f "${CONTAINERS[@]}" >/dev/null 2>&1 || true
  docker volume rm telegramsellbot_postgres_data telegramsellbot_redis_data >/dev/null 2>&1 || true
  rm -f "${NGINX_SITE_LINK}" "${NGINX_SITE_PATH}"
  systemctl reload nginx || true
  rm -rf "${PROJECT_DIR}"

  success "TelegramSellBot was removed from this server."
  exit 0
}

# ============================================================================
#  MAIN MENU
# ============================================================================
main_menu() {
  while true; do
    print_header
    echo -e "${BOLD}Deployment${NC}"
    echo -e "  ${BOLD}${GREEN}1)${NC} ⚡ Update & Deploy        ${DIM}(pull latest code + rebuild)${NC}"
    echo -e "  2) 🚀 Full Deploy            ${DIM}(rebuild without pulling)${NC}"
    echo -e "  3) 🔄 Quick Reload           ${DIM}(restart containers only)${NC}"
    echo
    echo -e "${BOLD}Setup${NC}"
    echo -e "  4) ⚙️  Configure .env"
    echo -e "  5) 🔐 Install Prerequisites & SSL"
    echo
    echo -e "${BOLD}Inspection${NC}"
    echo -e "  6) 📜 Logs"
    echo -e "  7) 📊 Service Status"
    echo -e "  ${BOLD}${GREEN}8)${NC} 🩺 Doctor               ${DIM}(self-diagnose + auto-fix)${NC}"
    echo
    echo -e "${BOLD}Data${NC}"
    echo -e "  9) 🗄️  Database Tools"
    echo -e "  10) 💾 Backup / Restore"
    echo
    echo -e "${BOLD}Danger${NC}"
    echo -e "  ${RED}11)${NC} 🗑️  Full Uninstall"
    echo
    echo -e "  0) Exit"
    echo
    read -r -p "$(echo -e "${BOLD}Choose an option: ${NC}")" choice
    case "${choice}" in
      1)  update_and_deploy ;;
      2)  full_deploy ;;
      3)  quick_reload ;;
      4)  setup_env_builder ;;
      5)  install_prerequisites_and_ssl ;;
      6)  logs_menu ;;
      7)  service_status ;;
      8)  doctor_run ;;
      9)  database_tools_menu ;;
      10) backup_menu ;;
      11) full_uninstall ;;
      0)  success "Goodbye."; exit 0 ;;
      *)  warn "Invalid option."; pause ;;
    esac
  done
}

trap 'error "Installer failed on line ${LINENO}."; exit 1' ERR

require_root "$@"
main_menu
