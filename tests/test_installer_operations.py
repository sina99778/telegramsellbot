"""Real Git updater tests and isolated deploy command-stub tests.

No production containers, database, apt or host configuration are touched.
"""
from __future__ import annotations

import gzip
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") if os.name != "nt" else "C:/Program Files/Git/bin/bash.exe"
pytestmark = pytest.mark.skipif(not BASH or not Path(BASH).exists(), reason="Bash required")


def bash(script, *, cwd=None, env=None):
    return subprocess.run(
        [BASH, "--noprofile", "--norc", "-c", script], cwd=cwd,
        env={**os.environ, **(env or {})}, text=True, capture_output=True, timeout=25,
    )


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], text=True, capture_output=True, timeout=20,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def init_repo(repo):
    repo.mkdir()
    git(repo, "init", "-b", "master")
    git(repo, "config", "user.email", "fixture@example.invalid")
    git(repo, "config", "user.name", "Fixture")
    (repo / "tracked.txt").write_text("initial", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")


@pytest.fixture
def remote_repo(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=master")
    seed = tmp_path / "seed"
    init_repo(seed)
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "origin", "master")
    git(seed, "checkout", "-b", "main")
    (seed / "tracked.txt").write_text("main-only", encoding="utf-8")
    git(seed, "commit", "-am", "main branch")
    git(seed, "push", "origin", "main")
    clone = tmp_path / "operator"
    git(tmp_path, "clone", "--depth=1", "--single-branch", "--branch=main",
        remote.as_uri(), str(clone))
    git(clone, "config", "user.email", "fixture@example.invalid")
    git(clone, "config", "user.name", "Fixture")
    return remote, seed, clone


def update(repo, **env):
    helper = shlex.quote((ROOT / "scripts/ops-common.sh").as_posix())
    command = f"set -euo pipefail; source {helper}; ops_fetch_code {shlex.quote(repo.as_posix())}"
    return bash(command, env={"TSB_DEPLOY_BRANCH": "", **env})


def test_default_branch_update_preserves_edits_and_operational_files(remote_repo):
    remote, _, clone = remote_repo
    (clone / "tracked.txt").write_text("local committed", encoding="utf-8")
    git(clone, "commit", "-am", "operator commit")
    old_sha = git(clone, "rev-parse", "HEAD")
    (clone / "tracked.txt").write_text("local dirty", encoding="utf-8")
    (clone / "operator-note.txt").write_text("untracked edit", encoding="utf-8")
    preserved = [".env", ".env.bak.keep", "installer.log", "ready_configs/token.txt",
                 "backups/data.sql.gz"]
    for name in preserved:
        path = clone / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("operational secret", encoding="utf-8")
    result = update(clone)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(clone, "branch", "--show-current") == "master"
    assert git(clone, "rev-parse", "HEAD") == git(remote, "rev-parse", "master")
    assert old_sha in git(clone, "for-each-ref", "--format=%(objectname)", "refs/tsb-backups")
    assert "local dirty" in git(clone, "show", "stash@{0}:tracked.txt")
    assert "untracked edit" in git(clone, "show", "stash@{0}^3:operator-note.txt")
    assert git(clone, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/master"
    for name in preserved:
        assert (clone / name).read_text(encoding="utf-8") == "operational secret"
    assert ".env" not in git(clone, "ls-tree", "-r", "--name-only", "stash@{0}^3").splitlines()


def test_explicit_branch_override_and_repeat_update(remote_repo):
    _, _, clone = remote_repo
    for _ in range(2):
        result = update(clone, TSB_DEPLOY_BRANCH="main")
        assert result.returncode == 0, result.stderr
        assert git(clone, "branch", "--show-current") == "main"
    refspecs = git(clone, "config", "--get-all", "remote.origin.fetch").splitlines()
    assert len(refspecs) == len(set(refspecs))


@pytest.mark.parametrize("branch", ["missing", "--bad", "bad branch"])
def test_failed_branch_resolution_leaves_worktree_untouched(remote_repo, branch):
    _, _, clone = remote_repo
    old_sha = git(clone, "rev-parse", "HEAD")
    (clone / "tracked.txt").write_text("do not lose", encoding="utf-8")
    result = update(clone, TSB_DEPLOY_BRANCH=branch)
    assert result.returncode != 0
    assert git(clone, "rev-parse", "HEAD") == old_sha
    assert (clone / "tracked.txt").read_text() == "do not lose"
    assert not git(clone, "stash", "list")


DOCKER_STUB = r'''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TSB_TEST_EVENTS"
case "$*" in
  'compose version') exit 0 ;;
  'info') exit 0 ;;
  *'config --quiet') [[ "${FAIL_AT:-}" != config ]]; exit $? ;;
  *'build api') [[ "${FAIL_AT:-}" != build ]]; exit $? ;;
  *'test -s /app/dashboard/dist/index.html') [[ "${FAIL_AT:-}" != spa ]]; exit $? ;;
  *pg_isready*) exit 0 ;;
  *pg_dump*) echo '-- fixture SQL'; [[ "${FAIL_AT:-}" != backup ]]; exit $? ;;
  *'python scripts/migrations/'*) [[ "${FAIL_AT:-}" != migration ]]; exit $? ;;
  inspect*)
    case "$3" in
      *State.Status*) echo "${MOCK_STATUS:-running}" ;;
      *State.Health*) echo healthy ;;
      *) echo "${MOCK_REVISION:-$TSB_BUILD_REVISION}" ;;
    esac
    exit 0 ;;
  *curl*) [[ "${FAIL_AT:-}" != probe ]]; exit $? ;;
esac
exit 0
'''


@pytest.fixture
def deploy_fixture(tmp_path):
    repo = tmp_path / "deploy"
    init_repo(repo)
    for name in ["deploy.sh", "docker-compose.prod.yml", "scripts/ops-common.sh"]:
        target = repo / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    migration_dir = repo / "scripts/migrations"
    migration_dir.mkdir()
    (migration_dir / "001_fixture.py").write_text("# fixture only")
    (repo / ".env").write_text(
        "POSTGRES_USER='fixture_user'\nPOSTGRES_DB=fixture_db\n"
        "TOKEN=secret-do-not-print\nUNUSED=$(touch injected)\n"
    )
    bins = tmp_path / "bin"
    bins.mkdir()
    tools = [("docker", DOCKER_STUB),
             ("flock", '#!/usr/bin/env bash\n[[ "${FAIL_AT:-}" != lock ]]\n')]
    for name, source in tools:
        tool = bins / name
        tool.write_text(source, encoding="utf-8", newline="\n")
        tool.chmod(0o755)
    events = tmp_path / "events"
    env = {"PATH": bins.as_posix() + (";" if os.name == "nt" else ":") + os.environ["PATH"],
           "TSB_LOCK_FILE": (tmp_path / "deploy.lock").as_posix(),
           "TSB_TEST_EVENTS": events.as_posix(), "TSB_HEALTH_TIMEOUT": "1"}
    return repo, env, events


def deploy(fixture, mode="full", **overrides):
    repo, env, events = fixture
    result = bash(f"bash ./deploy.sh {shlex.quote(mode)}", cwd=repo, env={**env, **overrides})
    return result, events.read_text() if events.exists() else ""


def test_full_deploy_backups_and_verifies_revision(deploy_fixture):
    repo, _, _ = deploy_fixture
    result, events = deploy(deploy_fixture)
    assert result.returncode == 0, result.stdout + result.stderr
    assert events.index("build api") < events.index("stop autoheal api bot worker")
    migration_index = events.index("python scripts/migrations/")
    assert events.index("pg_dump") < events.index("init_database") < migration_index
    assert migration_index < events.index("up -d --no-build api bot worker")
    assert "restart postgres" not in events and "down" not in events
    assert "--no-deps -T" in events
    assert '-U "$POSTGRES_USER" -d "$POSTGRES_DB"' in events
    dumps = list((repo / "backups").glob("*.sql.gz"))
    assert len(dumps) == 1 and b"fixture SQL" in gzip.decompress(dumps[0].read_bytes())
    assert len(list((repo / "backups").glob("*.env"))) == 1
    assert (repo / ".deployed-revision").read_text().strip() == git(repo, "rev-parse", "HEAD")
    assert "secret-do-not-print" not in result.stdout + result.stderr
    assert not (repo / "injected").exists()


@pytest.mark.parametrize("failure", ["lock", "config", "build", "spa", "backup", "migration"])
def test_deploy_stops_on_failure(deploy_fixture, failure):
    repo, _, _ = deploy_fixture
    result, events = deploy(deploy_fixture, FAIL_AT=failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (repo / ".deployed-revision").exists()
    assert "up -d --no-build api bot worker" not in events
    if failure in {"lock", "config", "build", "spa"}:
        assert "stop autoheal api bot worker" not in events
    if failure == "backup":
        assert "init_database" not in events
        assert not list((repo / "backups").glob("*.sql.gz"))
        assert "start api bot worker autoheal" in events
    if failure == "migration":
        assert "start api bot worker autoheal" not in events


@pytest.mark.parametrize("mode", ["full", "check"])
def test_stale_image_cannot_report_deployment_success(deploy_fixture, mode):
    result, _ = deploy(deploy_fixture, mode, MOCK_REVISION="old-image")
    assert result.returncode != 0
    assert "different image revision" in result.stderr


def test_check_is_read_only_and_reload_warns_about_stale_source(deploy_fixture):
    result, events = deploy(deploy_fixture, "check")
    assert result.returncode == 0, result.stderr
    mutations = ["build api", "pg_dump", "stop ", "restart ", "up -d"]
    assert all(word not in events for word in mutations)
    result, events = deploy(deploy_fixture, "reload", MOCK_REVISION="old-image")
    assert result.returncode == 0, result.stderr
    assert "Use Update & Deploy" in result.stdout
    assert "restart api bot worker" in events
    assert "build api" not in events


@pytest.mark.parametrize("overrides", [{"MOCK_STATUS": "exited"}, {"FAIL_AT": "probe"}])
def test_health_failure_cannot_report_success(deploy_fixture, overrides):
    result, _ = deploy(deploy_fixture, **overrides)
    assert result.returncode != 0
    assert "Deployment verified:" not in result.stdout


def test_invalid_mode_fails_before_docker(deploy_fixture):
    result, events = deploy(deploy_fixture, "unknown")
    assert result.returncode == 2 and not events


@pytest.mark.parametrize("name", ["setup.sh", "install.sh", "deploy.sh", "scripts/ops-common.sh"])
def test_shell_syntax(name):
    result = subprocess.run([BASH, "-n", str(ROOT / name)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt" or not shutil.which("flock"), reason="Linux flock required")
def test_real_lock_inheritance_and_competing_process(tmp_path):
    helper = shlex.quote((ROOT / "scripts/ops-common.sh").as_posix())
    env = {**os.environ, "TSB_LOCK_FILE": (tmp_path / "operation.lock").as_posix()}
    child = shlex.quote(f"source {helper}; ops_acquire_lock; echo nested-ready")
    command = f"set -e; source {helper}; ops_acquire_lock; bash -c {child}; read -r release"
    holder = subprocess.Popen(
        [BASH, "-c", command],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "nested-ready"
        competing = bash(f"source {helper}; ops_acquire_lock", env=env)
        assert competing.returncode != 0 and "Another install/update/deploy" in competing.stderr
    finally:
        holder.communicate("release\n", timeout=10)
    assert bash(f"source {helper}; ops_acquire_lock", env=env).returncode == 0


@pytest.mark.skipif(os.name == "nt" or not shutil.which("rsync"), reason="Linux rsync required")
def test_snapshot_sync_preserves_runtime_files(tmp_path):
    source = tmp_path / "snapshot"
    target = tmp_path / "installation"
    source.mkdir()
    target.mkdir()
    (source / "new-source.py").write_text("new code")
    (source / ".env.example").write_text("new example")
    (target / "stale-source.py").write_text("old code")
    protected = [".env", ".env.bak", ".env.bak.2026", "backups/db.sql.gz", "ready_configs/a.txt",
                 "installer.log", ".deployed-revision", ".git/HEAD", "dashboard/dist/index.html"]
    for name in protected:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve me")
    helper = shlex.quote((ROOT / "scripts/ops-common.sh").as_posix())
    src, dst = shlex.quote(source.as_posix()), shlex.quote(target.as_posix())
    result = bash(f"source {helper}; ops_sync_snapshot {src} {dst}")
    assert result.returncode == 0, result.stderr
    for name in protected:
        assert (target / name).read_text() == "preserve me"
    assert (target / "new-source.py").read_text() == "new code"
    assert (target / ".env.example").read_text() == "new example"
    assert not (target / "stale-source.py").exists()
