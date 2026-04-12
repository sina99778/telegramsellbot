#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE_FILE="${PROJECT_DIR}/docker-compose.prod.yml"
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

generate_fernet_key() {
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

prompt_required() {
  local prompt_text="$1"
  local var_name="$2"
  local is_secret="${3:-0}"
  local value=""

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

  printf -v "${var_name}" '%s' "${value}"
}

setup_env_builder() {
  print_header
  info "Building a fresh .env file."
  require_commands python3

  local bot_token owner_telegram_id xui_base_url xui_username xui_password nowpayments_api_key domain_name
  local app_secret_key postgres_password redis_password postgres_user postgres_db database_url redis_url webhook_url support_url

  prompt_required "Bot Token" bot_token 1
  prompt_required "Admin Telegram ID" owner_telegram_id
  prompt_required "X-UI Base URL (e.g. http://ip:2053)" xui_base_url
  prompt_required "X-UI Username" xui_username
  prompt_required "X-UI Password" xui_password 1
  prompt_required "NOWPayments API Key" nowpayments_api_key 1
  prompt_required "Domain Name (for webhook/API)" domain_name

  app_secret_key="$(generate_fernet_key)"
  postgres_password="$(generate_password)"
  redis_password="$(generate_password)"
  postgres_user="telegramsellbot"
  postgres_db="telegramsellbot"
  database_url="postgresql+asyncpg://${postgres_user}:${postgres_password}@postgres:5432/${postgres_db}"
  redis_url="redis://default:${redis_password}@redis:6379/0"
  webhook_url="https://${domain_name}/api/webhooks/nowpayments"
  support_url="https://${domain_name}"

  cat > "${ENV_FILE}" <<EOF
APP_ENV=production
APP_DEBUG=false
LOG_LEVEL=INFO
APP_SECRET_KEY=${app_secret_key}

BOT_TOKEN=${bot_token}
BOT_PARSE_MODE=HTML
BOT_DROP_PENDING_UPDATES=false
OWNER_TELEGRAM_ID=${owner_telegram_id}

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
NOWPAYMENTS_IPN_CALLBACK_URL=${webhook_url}

WEB_BASE_URL=https://${domain_name}
SUPPORT_URL=${support_url}
EOF

  success ".env created at ${ENV_FILE}"
  warn "Review the generated file before deployment, especially any optional variables you want to add."
  pause
}

install_prerequisites_and_ssl() {
  print_header
  info "Installing Docker, Compose plugin, Nginx, and Certbot on Ubuntu."

  export DEBIAN_FRONTEND=noninteractive
  apt-get update
  apt-get install -y ca-certificates curl gnupg lsb-release nginx certbot python3-certbot-nginx

  if ! command -v docker >/dev/null 2>&1; then
    apt-get install -y docker.io
  fi

  if ! docker compose version >/dev/null 2>&1; then
    apt-get install -y docker-compose-plugin || true
  fi

  systemctl enable --now docker
  systemctl enable --now nginx

  local email domain_name
  prompt_required "Email for Let's Encrypt" email
  prompt_required "Domain pointing to this server" domain_name

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

  certbot --nginx --non-interactive --redirect --agree-tos -m "${email}" -d "${domain_name}"

  success "Prerequisites installed and SSL configured for ${domain_name}"
  pause
}

deploy_bot() {
  print_header
  info "Deploying containers with ${COMPOSE_FILE}"

  if [[ ! -f "${ENV_FILE}" ]]; then
    error ".env file not found. Run the configuration builder first."
    pause
    return
  fi

  if [[ ! -x "${PROJECT_DIR}/deploy.sh" ]]; then
    chmod +x "${PROJECT_DIR}/deploy.sh"
  fi

  (cd "${PROJECT_DIR}" && ./deploy.sh)
  success "Deployment completed."
  pause
}

main_menu() {
  while true; do
    print_header
    echo "1) Setup Configuration (.env builder)"
    echo "2) Install Prerequisites & SSL (Nginx + Certbot)"
    echo "3) Deploy Bot"
    echo "0) Exit"
    echo
    read -r -p "Choose an option: " choice

    case "${choice}" in
      1) setup_env_builder ;;
      2) install_prerequisites_and_ssl ;;
      3) deploy_bot ;;
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
