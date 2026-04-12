#!/usr/bin/env bash
set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

SETUP_VERSION="2026-04-12-1"
REPO_URL="https://github.com/sina99778/telegramsellbot.git"
INSTALL_DIR="/opt/telegramsellbot"
ENV_BACKUP="/tmp/telegramsellbot.env.backup"

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

info "Running setup.sh version ${SETUP_VERSION}"

export DEBIAN_FRONTEND=noninteractive

info "Updating package index and installing git + curl..."
apt-get update -qq
apt-get install -y -qq git curl >/dev/null

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  info "Existing installation found in ${INSTALL_DIR}. Replacing it with a fresh clone..."
  if [[ -f "${INSTALL_DIR}/.env" ]]; then
    cp "${INSTALL_DIR}/.env" "${ENV_BACKUP}"
    info "Existing .env backed up to ${ENV_BACKUP}"
  fi
  rm -rf "${INSTALL_DIR}"
  git clone --branch main --single-branch "${REPO_URL}" "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"
  if [[ -f "${ENV_BACKUP}" ]]; then
    mv "${ENV_BACKUP}" "${INSTALL_DIR}/.env"
    info "Previous .env restored."
  fi
else
  info "Cloning repository into ${INSTALL_DIR}..."
  rm -rf "${INSTALL_DIR}"
  git clone --branch main --single-branch "${REPO_URL}" "${INSTALL_DIR}"
  cd "${INSTALL_DIR}"
fi

chmod +x install.sh deploy.sh

success "Launching interactive installer..."
exec bash install.sh
