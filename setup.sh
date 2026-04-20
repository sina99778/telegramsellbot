#!/usr/bin/env bash
set -Eeuo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

SETUP_VERSION="2026-04-12-3"
REPO_URL="https://github.com/sina99778/telegramsellbot.git"
REPO_BRANCH="master"
INSTALL_DIR="/opt/telegramsellbot"
TMP_DIR="/tmp/telegramsellbot-sync"
MODE="${1:-install}"

error() {
  echo -e "${RED}[ERROR]${NC} $*" >&2
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

cleanup() {
  rm -rf "${TMP_DIR}"
}

sync_project_files() {
  rm -rf "${TMP_DIR}"
  info "Fetching latest repository snapshot..."
  git clone --depth 1 --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${TMP_DIR}"

  mkdir -p "${INSTALL_DIR}"
  info "Syncing project files into ${INSTALL_DIR} without touching your .env ..."

  rsync -a --delete \
    --exclude ".env" \
    --exclude ".git" \
    --exclude "__pycache__" \
    --exclude "*.pyc" \
    "${TMP_DIR}/" "${INSTALL_DIR}/"

  if [[ ! -d "${INSTALL_DIR}/.git" ]]; then
    info "Installing git metadata for future updates..."
    cp -R "${TMP_DIR}/.git" "${INSTALL_DIR}/.git"
  else
    info "Refreshing local git metadata..."
    rsync -a --delete "${TMP_DIR}/.git/" "${INSTALL_DIR}/.git/"
  fi

  cd "${INSTALL_DIR}"
  chmod +x setup.sh install.sh deploy.sh
}

trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
  error "This script must be run as root. Please use sudo."
  exit 1
fi

if [[ "${PWD}" == "${INSTALL_DIR}" || "${PWD}" == "${INSTALL_DIR}/"* ]]; then
  info "Current shell is inside ${INSTALL_DIR}; switching to /root for safe self-update."
  cd /root
fi

info "Running setup.sh version ${SETUP_VERSION}"

export DEBIAN_FRONTEND=noninteractive

info "Updating package index and installing git + curl + rsync..."
apt-get update -qq
apt-get install -y -qq git curl rsync >/dev/null

case "${MODE}" in
  install|--install)
    sync_project_files
    success "Project files are up to date."
    success "Launching interactive installer..."
    exec bash install.sh
    ;;
  sync-only|--sync-only|update|--update)
    sync_project_files
    success "Project files were updated successfully."
    exit 0
    ;;
  *)
    error "Unknown mode: ${MODE}. Supported modes: install, sync-only"
    exit 1
    ;;
esac
