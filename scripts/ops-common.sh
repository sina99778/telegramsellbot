#!/usr/bin/env bash
# Shared by the interactive updater and deploy.sh. Never source .env as shell code.

ops_acquire_lock() {
  local lock_file="${TSB_LOCK_FILE:-/run/telegramsellbot-update.lock}"
  command -v flock >/dev/null 2>&1 || {
    echo "flock is required (install util-linux)." >&2; return 1;
  }
  # A deploy launched by the updater inherits descriptor 9 and its lock.
  # Check the actual descriptor, rather than trusting an environment flag.
  if [[ "$(readlink "/proc/${BASHPID}/fd/9" 2>/dev/null || true)" != "${lock_file}" ]]; then
    exec 9>"${lock_file}" || return 1
  fi
  flock -n 9 || {
    echo "Another install/update/deploy is running. Wait for it to finish." >&2
    return 1
  }
}

ops_deploy_branch() {
  local repo="$1" branch="${TSB_DEPLOY_BRANCH:-}"
  if [[ -z "${branch}" ]]; then
    branch="$(git -C "${repo}" ls-remote --symref origin HEAD | awk '$1 == "ref:" && $3 == "HEAD" {sub("^refs/heads/", "", $2); print $2}')" || return 1
  fi
  if [[ -z "${branch}" ]] || ! git check-ref-format --branch "${branch}" >/dev/null 2>&1; then
    echo "Cannot resolve deployment branch. Set TSB_DEPLOY_BRANCH explicitly." >&2
    return 1
  fi
  printf '%s\n' "${branch}"
}

ops_fetch_code() {
  local repo="$1" branch target backup_ref
  branch="$(ops_deploy_branch "${repo}")" || return 1
  target="refs/remotes/origin/${branch}"
  echo "Deployment source: origin/${branch}"
  # Explicit refspec also works for shallow, single-branch installations.
  if ! git -C "${repo}" config --get-all remote.origin.fetch | grep -Fxq "+refs/heads/${branch}:${target}"; then
    git -C "${repo}" remote set-branches --add origin "${branch}" || return 1
  fi
  git -C "${repo}" fetch origin "+refs/heads/${branch}:${target}" || return 1
  git -C "${repo}" rev-parse --verify "${target}^{commit}" >/dev/null || return 1

  # Keep both committed and uncommitted operator changes recoverable. Do not
  # git-clean runtime directories, backups or secret snapshots.
  backup_ref="refs/tsb-backups/pre-update-$(date -u +%Y%m%dT%H%M%S%N)-$$"
  git -C "${repo}" update-ref "${backup_ref}" HEAD '' || return 1
  git -C "${repo}" stash push --include-untracked -m "TSB pre-update ${backup_ref}" -- . \
    ':(exclude)backups' ':(exclude)ready_configs' ':(exclude).env' \
    ':(exclude).env.bak*' ':(exclude)*.log' || return 1
  echo "Previous commit preserved at ${backup_ref}; local edits, if any, are in git stash list."
  git -C "${repo}" checkout -B "${branch}" "${target}" || return 1
  git -C "${repo}" branch --set-upstream-to="origin/${branch}" "${branch}" || return 1
  echo "Source version: $(git -C "${repo}" rev-parse --short HEAD) (${branch})"
}

ops_sync_snapshot() {
  local snapshot="$1" destination="$2"
  mkdir -p "${destination}" || return 1
  rsync -a --delete \
    --exclude '/.env' --exclude '/.env.bak*' --exclude '/.git' \
    --exclude '/backups/' --exclude '/ready_configs/' --exclude '*.log' \
    --exclude '/.deployed-revision' --exclude '/.venv/' \
    --exclude '/dashboard/node_modules/' --exclude '/dashboard/dist/' \
    --exclude '__pycache__/' --exclude '*.pyc' \
    "${snapshot}/" "${destination}/"
}
