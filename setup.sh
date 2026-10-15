#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

SETUP_VERSION="2026-10-15-1"
REPO_URL="https://github.com/sina99778/telegramsellbot.git"
REPO_BRANCH="${TSB_DEPLOY_BRANCH:-}"
INSTALL_DIR="/opt/telegramsellbot"
TMP_DIR=""
MODE="${1:-install}"
case "${MODE}" in
  install|--install|sync-only|--sync-only|update|--update) ;;
  *) echo "Usage: setup.sh [install|sync-only]" >&2; exit 2 ;;
esac
error() { echo "[ERROR] $*" >&2; }
info() { echo "[INFO] $*"; }
cleanup() {
  # Only a directory created by this process; never a fixed shared /tmp path.
  if [[ -n "${TMP_DIR}" && "${TMP_DIR}" == /tmp/telegramsellbot-sync.* ]]; then
    rm -rf -- "${TMP_DIR}"
  fi
}
trap cleanup EXIT
trap 'error "Setup failed on line ${LINENO}. The installer was not launched."; exit 1' ERR
main() {
  [[ "${EUID}" -eq 0 ]] || { error "Run setup as root (sudo)."; exit 1; }
  cd /root
  info "Setup ${SETUP_VERSION}"

  missing=()
  for tool in git curl rsync flock; do
    command -v "${tool}" >/dev/null 2>&1 || missing+=("${tool}")
  done
  if (( ${#missing[@]} )); then
    command -v apt-get >/dev/null 2>&1 || { error "Ubuntu/Debian with apt-get is required for bootstrap."; exit 1; }
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o Acquire::Retries=3 update -qq
    apt-get -o Acquire::Retries=3 install -y -qq ca-certificates git curl rsync util-linux
  fi

  # The same lock is shared with interactive updates and direct deploys.
  if [[ "$(readlink "/proc/${BASHPID}/fd/9" 2>/dev/null || true)" != /run/telegramsellbot-update.lock ]]; then
    exec 9>/run/telegramsellbot-update.lock
  fi
  flock -n 9 || { error "Another install/update/deploy is running."; exit 1; }
  TMP_DIR="$(mktemp -d /tmp/telegramsellbot-sync.XXXXXX)"
  if [[ -z "${REPO_BRANCH}" ]]; then
    REPO_BRANCH="$(git ls-remote --symref "${REPO_URL}" HEAD | awk '$1 == "ref:" && $3 == "HEAD" {sub("^refs/heads/", "", $2); print $2}')"
  fi
  git check-ref-format --branch "${REPO_BRANCH}" >/dev/null || { error "Invalid deployment branch."; exit 1; }
  info "Fetching ${REPO_BRANCH} from GitHub..."
  git clone --depth 1 --branch "${REPO_BRANCH}" --single-branch "${REPO_URL}" "${TMP_DIR}/repo"
  source "${TMP_DIR}/repo/scripts/ops-common.sh"

  if [[ -d "${INSTALL_DIR}/.git" ]]; then
    # Preserve local commits/edits instead of replacing .git with a shallow clone.
    ops_fetch_code "${INSTALL_DIR}"
  else
    info "Syncing code; preserving environment, backups, uploaded configs and logs."
    ops_sync_snapshot "${TMP_DIR}/repo" "${INSTALL_DIR}"
    cp -R "${TMP_DIR}/repo/.git" "${INSTALL_DIR}/.git"
  fi
  cd "${INSTALL_DIR}"
  chmod +x setup.sh install.sh deploy.sh doctor.sh backup.sh restore.sh migrate_bundle.sh
  info "Source ready: $(git rev-parse --short HEAD) ($(git branch --show-current))"
  cleanup
  TMP_DIR=""
  # Release before handing control to the interactive menu.
  exec 9>&-
  case "${MODE}" in
    install|--install) exec bash install.sh ;;
    *) info "Source updated. Run bash deploy.sh full to build and deploy it." ;;
  esac

}

main "$@"
