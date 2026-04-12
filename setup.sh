#!/usr/bin/env bash
set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_URL="https://github.com/sina99778/telegramsellbot.git"
INSTALL_DIR="/opt/telegramsellbot"

error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
}

info() {
  echo -e "${BLUE}[INFO]${NC} $*"
}

success() {
  echo -e "${GREEN}[OK]${NC} $*"
}

if [[ "${EUID}" -ne 0 ]]; then
  error "This script must be run as root. Please use sudo."
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

info "Updating package index and installing git + curl..."
apt-get update -qq
apt-get install -y -qq git curl >/dev/null

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  info "Existing installation found in ${INSTALL_DIR}. Pulling latest code..."
  cd "${INSTALL_DIR}"
  git fetch origin
  git reset --hard
  git clean -fd
  git checkout -B main origin/main
  git pull --ff-only origin main
else
  info "Cloning repository into ${INSTALL_DIR}..."
  rm -rf "${INSTALL_DIR}"
  git clone --branch main --single-branch "${REPO_URL}" "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"
fi

chmod +x install.sh deploy.sh

success "Launching interactive installer..."
exec bash install.sh
