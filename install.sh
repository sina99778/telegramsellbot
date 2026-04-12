#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
SETUP_SCRIPT="${PROJECT_DIR}/setup.sh"
DEPLOY_SCRIPT="${PROJECT_DIR}/deploy.sh"
NGINX_SITE_PATH="/etc/nginx/sites-available/telegramsellbot.conf"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/telegramsellbot.conf"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_header() {
  clear
  echo -e "${BOLD}${CYAN}"
  echo "==============================================="
  echo "      TelegramSellBot Easy Installer"
  echo "==============================================="
  echo -e "${NC}"
}

info() {
  echo -e "${BLUE}[INFO]${NC} $*"
}

success() {
  echo -e "${GREEN}[OK]${NC} $*"
}

warn() {
  echo -e "${YELLOW}[WARN]${NC} $*"
}

error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

pause() {
  read -r -p "Press Enter to continue..."
}

require_root() {
  if [[ "${EUID}" -ne 0 ]]; then
    if command -v sudo >/dev/null 2>&1; then
      exec sudo -E bash "$0" "$@"
    else
      error "This installer requires root privileges and sudo is not installed."
      exit 1
    fi
  fi
}

require_commands() {
  local missing=0
  for cmd in "$@"; do
    if ! command -v "${cmd}" >/dev/null 2>&1; then
      error "Missing required command: ${cmd}"
      missing=1
    fi
  done
  if [[ "${missing}" -ne 0 ]]; then
    exit 1
  fi
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    echo "plugin"
  elif command -v docker-compose >/dev/null 2>&1; then
    echo "legacy"
  else
    echo "missing"
  fi
}

run_compose() {
  local mode
  mode="$(compose_cmd)"

  if [[ "${mode}" == "plugin" ]]; then
    docker compose -f "${COMPOSE_FILE}" "$@"
  elif [[ "${mode}" == "legacy" ]]; then
    docker-compose -f "${COMPOSE_FILE}" "$@"
  else
    error "Docker Compose is not installed."
    return 1
  fi
}

wait_for_postgres() {
  local db_user db_name attempt max_attempts
  db_user="$(read_env_value POSTGRES_USER)"
  db_name="$(read_env_value POSTGRES_DB)"
  db_user="${db_user:-telegramsellbot}"
  db_name="${db_name:-telegramsellbot}"
  max_attempts=30

  for attempt in $(seq 1 "${max_attempts}"); do
    if docker exec telegramsellbot-postgres pg_isready -U "${db_user}" -d "${db_name}" >/dev/null 2>&1; then
      success "PostgreSQL is ready."
      return 0
    fi
    info "Waiting for PostgreSQL to become ready (${attempt}/${max_attempts})..."
    sleep 2
  done

  error "PostgreSQL did not become ready in time."
  return 1
}

generate_fernet_key() {
  # Ensure cryptography is available for Fernet key generation
  if ! python3 -c "from cryptography.fernet import Fernet" 2>/dev/null; then
    info "Installing cryptography package for Fernet key generation..."
    pip3 install -q cryptography 2>/dev/null || apt-get install -y -qq python3-pip >/dev/null && pip3 install -q cryptography
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

read_env_value() {
  local key="$1"
  if [[ ! -f "${ENV_FILE}" ]]; then
    return 0
  fi

  local line
  line="$(grep -E "^${key}=" "${ENV_FILE}" | tail -n 1 || true)"
  if [[ -z "${line}" ]]; then
    return 0
  fi

  printf '%s' "${line#*=}"
}

prompt_with_default() {
  local prompt_text="$1"
  local var_name="$2"
  local default_value="${3:-}"
  local is_secret="${4:-0}"
  local value=""

  if [[ -n "${default_value}" ]]; then
    if [[ "${is_secret}" == "1" ]]; then
      read -r -s -p "${prompt_text} [press Enter to keep current]: " value
      echo
      if [[ -z "${value}" ]]; then
        value="${default_value}"
      fi
    else
      read -r -p "${prompt_text} [${default_value}]: " value
      if [[ -z "${value}" ]]; then
        value="${default_value}"
      fi
    fi
  else
    while [[ -z "${value}" ]]; do
      if [[ "${is_secret}" == "1" ]]; then
        read -r -s -p "${prompt_text}: " value
        echo
      else
        read -r -p "${prompt_text}: " value
      fi
      if [[ -z "${value}" ]]; then
        warn "This value is required."
      fi
    done
  fi

  printf -v "${var_name}" '%s' "${value}"
}

extract_domain_from_url() {
  local url="$1"
  url="${url#http://}"
  url="${url#https://}"
  printf '%s' "${url%%/*}"
}

setup_env_builder() {
  print_header
  info "Building or updating .env intelligently."
  require_commands python3

  local current_bot_token current_owner current_xui_base_url current_xui_username current_xui_password
  local current_nowpayments_api_key current_domain current_app_secret current_postgres_password
  local current_redis_password current_admin_api_key current_nowpayments_ipn_secret
  local bot_token owner_telegram_id xui_base_url xui_username xui_password nowpayments_api_key domain_name
  local app_secret_key postgres_password redis_password admin_api_key nowpayments_ipn_secret
  local postgres_user postgres_db database_url redis_url webhook_url support_url web_base_url

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

  prompt_with_default "Bot Token" bot_token "${current_bot_token}" 1
  prompt_with_default "Admin Telegram ID" owner_telegram_id "${current_owner}"
  prompt_with_default "X-UI Base URL (e.g. http://ip:2053)" xui_base_url "${current_xui_base_url}"
  prompt_with_default "X-UI Username" xui_username "${current_xui_username}"
  prompt_with_default "X-UI Password" xui_password "${current_xui_password}" 1
  prompt_with_default "NOWPayments API Key" nowpayments_api_key "${current_nowpayments_api_key}" 1
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
  support_url="https://${domain_name}"
  web_base_url="https://${domain_name}"

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

WEB_BASE_URL=${web_base_url}
SUPPORT_URL=${support_url}
EOF

  success ".env updated at ${ENV_FILE}"
  warn "Existing generated secrets were preserved automatically unless you changed them."
  pause
}

install_prerequisites_and_ssl() {
  print_header
  info "Installing or verifying Docker, Compose, Nginx, and Certbot on Ubuntu."

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg lsb-release nginx certbot python3-certbot-nginx

  if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y docker.io
  fi

  if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y docker-compose-plugin || true
  fi
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    apt-get install -y docker-compose || true
  fi

  if docker compose version >/dev/null 2>&1; then
    info "Using modern docker compose plugin."
  elif command -v docker-compose >/dev/null 2>&1; then
    warn "Only legacy docker-compose is available. Deploy script will use compatibility cleanup mode."
  else
    error "Neither docker compose plugin nor docker-compose could be installed."
    pause
    return
  fi

  systemctl enable --now docker
  systemctl enable --now nginx

  local current_domain current_email email domain_name
  current_domain="$(extract_domain_from_url "$(read_env_value WEB_BASE_URL)")"
  current_email=""

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

  certbot --nginx --non-interactive --redirect --agree-tos -m "${email}" -d "${domain_name}" || {
    error "Certbot failed. Make sure the domain points to this VPS and port 80/443 are open."
    pause
    return
  }

  success "Prerequisites installed and SSL configured for ${domain_name}"
  pause
}

full_deploy() {
  print_header
  info "Running full deploy / rebuild..."

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found. Run the configuration builder first."
    pause
    return
  fi

  chmod +x "${DEPLOY_SCRIPT}"
  (cd "${PROJECT_DIR}" && "${DEPLOY_SCRIPT}" full)
  success "Full deployment completed."
  pause
}

quick_reload() {
  print_header
  info "Quick reloading app services..."

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found. Run the configuration builder first."
    pause
    return
  fi

  chmod +x "${DEPLOY_SCRIPT}"
  (cd "${PROJECT_DIR}" && "${DEPLOY_SCRIPT}" reload)
  success "Quick reload completed."
  pause
}

show_important_logs() {
  print_header
  local mode
  mode="$(compose_cmd)"
  if [[ "${mode}" == "missing" ]]; then
    error "Docker Compose is not installed."
    pause
    return
  fi

  echo "1) Bot logs"
  echo "2) API logs"
  echo "3) Worker logs"
  echo "4) Postgres logs"
  echo "5) Redis logs"
  echo "6) All important logs"
  echo "0) Back"
  echo

  read -r -p "Choose logs to show: " log_choice
  case "${log_choice}" in
    1) run_compose logs --tail=120 bot ;;
    2) run_compose logs --tail=120 api ;;
    3) run_compose logs --tail=120 worker ;;
    4) run_compose logs --tail=120 postgres ;;
    5) run_compose logs --tail=120 redis ;;
    6) run_compose logs --tail=80 bot api worker postgres redis ;;
    0) return ;;
    *) warn "Invalid option." ;;
  esac
  echo
  pause
}

smart_update() {
  print_header
  info "Updating project files from GitHub without touching .env ..."
  chmod +x "${SETUP_SCRIPT}"
  (cd /root && "${SETUP_SCRIPT}" sync-only)
  success "Project files updated."
  pause
}

full_uninstall() {
  print_header
  warn "This will stop the stack, remove containers, remove volumes, remove the project directory, and remove the Nginx site config."
  warn "Let's Encrypt certificate files are NOT deleted automatically."
  echo
  read -r -p "Type DELETE to continue: " confirm
  if [[ "${confirm}" != "DELETE" ]]; then
    warn "Uninstall cancelled."
    pause
    return
  fi

  local mode
  mode="$(compose_cmd)"
  if [[ "${mode}" != "missing" && -f "${COMPOSE_FILE}" ]]; then
    run_compose down -v --remove-orphans || true
  fi

  docker rm -f telegramsellbot-postgres telegramsellbot-redis telegramsellbot-api telegramsellbot-bot telegramsellbot-worker >/dev/null 2>&1 || true
  docker volume rm telegramsellbot_postgres_data telegramsellbot_redis_data >/dev/null 2>&1 || true
  rm -f "${NGINX_SITE_LINK}" "${NGINX_SITE_PATH}"
  systemctl reload nginx || true
  rm -rf "${PROJECT_DIR}"

  success "TelegramSellBot was removed from this server."
  exit 0
}

db_status() {
  print_header
  info "Database containers and volumes status"
  echo
  docker ps -a --format "table {{.Names}}\t{{.Status}}" | grep telegramsellbot || true
  echo
  docker volume ls --format "table {{.Name}}" | grep telegramsellbot || true
  echo
  pause
}

db_bootstrap_schema() {
  print_header
  info "Bootstrapping database schema..."
  info "Rebuilding API image to ensure bootstrap uses the latest code..."
  run_compose build api
  run_compose run --rm api python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())"
  success "Database schema bootstrap completed."
  pause
}

db_restart_postgres() {
  print_header
  info "Restarting PostgreSQL service..."
  run_compose restart postgres
  success "PostgreSQL restarted."
  pause
}

db_reset_database() {
  print_header
  warn "This will DELETE all PostgreSQL data for TelegramSellBot."
  warn "Only use this if you do not need existing users, orders, wallets, tickets, or broadcasts."
  echo
  read -r -p "Type RESET to continue: " confirm
  if [[ "${confirm}" != "RESET" ]]; then
    warn "Database reset cancelled."
    pause
    return
  fi

  run_compose down -v --remove-orphans || true
  docker volume rm telegramsellbot_postgres_data >/dev/null 2>&1 || true
  success "Database volume removed."
  info "Recreating postgres and redis..."
  run_compose up -d postgres redis
  wait_for_postgres
  info "Rebuilding API image to ensure bootstrap uses the latest code..."
  run_compose build api
  info "Bootstrapping schema..."
  run_compose run --rm api python -c "import asyncio; import models; from core.database import init_database; asyncio.run(init_database())"
  success "Database reset and bootstrap completed."
  pause
}

database_tools_menu() {
  while true; do
    print_header
    echo "Database Tools"
    echo
    echo "1) Show DB Status"
    echo "2) Bootstrap Schema"
    echo "3) Restart PostgreSQL"
    echo "4) Reset Database (Delete All Data)"
    echo "0) Back"
    echo
    read -r -p "Choose an option: " db_choice

    case "${db_choice}" in
      1) db_status ;;
      2) db_bootstrap_schema ;;
      3) db_restart_postgres ;;
      4) db_reset_database ;;
      0) return ;;
      *)
        warn "Invalid option."
        pause
        ;;
    esac
  done
}

main_menu() {
  while true; do
    print_header
    echo "1) Setup Configuration (.env builder)"
    echo "2) Install Prerequisites & SSL (Nginx + Certbot)"
    echo "3) Full Deploy / Rebuild"
    echo "4) Quick Reload Services"
    echo "5) Show Important Logs"
    echo "6) Smart Update Project Files"
    echo "7) Database Tools"
    echo "8) Full Uninstall"
    echo "0) Exit"
    echo
    read -r -p "Choose an option: " choice

    case "${choice}" in
      1) setup_env_builder ;;
      2) install_prerequisites_and_ssl ;;
      3) full_deploy ;;
      4) quick_reload ;;
      5) show_important_logs ;;
      6) smart_update ;;
      7) database_tools_menu ;;
      8) full_uninstall ;;
      0)
        success "Goodbye."
        exit 0
        ;;
      *)
        warn "Invalid option."
        pause
        ;;
    esac
  done
}

trap 'error "Installer failed on line ${LINENO}."; exit 1' ERR

require_root "$@"
main_menu
