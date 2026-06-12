"""
Regression tests for deep-debug finding #78 (low severity).

Bug 1: apps/worker/jobs/backup.py:_dump_postgres hand-parsed
settings.database_url with string splits — a percent-encoded password
(e.g. %40 for '@', required in a DSN for any password with special
characters) reached PGPASSWORD still encoded, and a query string
(?sslmode=require etc.) stayed glued onto the dbname, so pg_dump
targeted a nonexistent database. Fix: parse with urllib.parse
(urlsplit + unquote).

Bug 2: because backup_last_run_at is only stamped on success, the
30-minute scheduler tick retried and re-DMed every admin on EVERY tick
while pg_dump kept failing (48 messages per admin per day). Fix: the
scheduled path throttles the failure alert to once per
_FAILURE_NOTIFY_MIN_INTERVAL_SECONDS per worker process; manual and
forced runs always alert, and a success resets the throttle.

Mock-based only — no DB, no network, no subprocess.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.worker.jobs import backup as backup_mod


ADMIN_IDS = {111, 222}


# ─── helpers ─────────────────────────────────────────────────────────────


def _mock_proc(returncode: int = 0, stdout: bytes = b"SQLDUMP"):
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, b""))
    return proc


def _patch_dsn_and_exec(dsn: str, proc=None):
    """Patch settings.database_url and the subprocess spawn; return (stack, exec_mock)."""
    stack = ExitStack()
    stack.enter_context(patch.object(
        backup_mod, "settings", SimpleNamespace(database_url=dsn)))
    exec_mock = AsyncMock(return_value=proc or _mock_proc())
    stack.enter_context(patch.object(
        backup_mod.asyncio, "create_subprocess_exec", exec_mock))
    return stack, exec_mock


def _cmd_opt(cmd: list[str], flag: str) -> str:
    """Return the value following `flag` in a pg_dump argv list."""
    return cmd[cmd.index(flag) + 1]


def _make_repo():
    repo = MagicMock()
    repo.get_backup_interval_hours = AsyncMock(return_value=6)
    repo.get_backup_last_run_iso = AsyncMock(return_value=None)
    repo.get_backup_channel_id = AsyncMock(return_value=-100123)
    repo.get_sales_report_chat_id = AsyncMock(return_value=None)
    repo.set_backup_last_run_now = AsyncMock()
    return repo


def _patch_run_backup_internals(repo, pg_dump_result):
    """Patch the heavy helpers used by run_backup; return the stack."""
    stack = ExitStack()
    stack.enter_context(patch.object(
        backup_mod, "AppSettingsRepository", MagicMock(return_value=repo)))
    stack.enter_context(patch.object(
        backup_mod, "_dump_postgres", AsyncMock(return_value=pg_dump_result)))
    stack.enter_context(patch.object(
        backup_mod, "_dump_xui_databases", AsyncMock(return_value=[])))
    stack.enter_context(patch.object(
        backup_mod, "_read_env_file", MagicMock(return_value=None)))
    stack.enter_context(patch.object(
        backup_mod, "_read_ready_configs_dir", MagicMock(return_value=None)))
    stack.enter_context(patch.object(
        backup_mod, "_get_admin_telegram_ids", AsyncMock(return_value=set(ADMIN_IDS))))
    stack.enter_context(patch.object(
        backup_mod, "_build_bundle", MagicMock(return_value=b"BUNDLE")))
    stack.enter_context(patch.object(
        backup_mod, "_run_git_sha", MagicMock(return_value="abc1234")))
    stack.enter_context(patch.object(
        backup_mod, "_run_git_branch", MagicMock(return_value="master")))
    return stack


@pytest.fixture
def bot():
    b = AsyncMock()
    b.send_document = AsyncMock()
    b.send_message = AsyncMock()
    return b


@pytest.fixture(autouse=True)
def _reset_failure_throttle():
    """Each test starts with a fresh (un-throttled) failure-alert state."""
    backup_mod._last_failure_notified_monotonic = None
    yield
    backup_mod._last_failure_notified_monotonic = None


# ─── _dump_postgres DSN parsing ──────────────────────────────────────────


async def test_percent_encoded_password_is_decoded_for_pgpassword():
    """%40 / %2F / %23 in the password must reach PGPASSWORD decoded."""
    dsn = "postgresql+asyncpg://vpn_user:p%40ss%2Fw0rd%23@db:5432/vpnbot"
    stack, exec_mock = _patch_dsn_and_exec(dsn)
    with stack:
        result = await backup_mod._dump_postgres()
    assert result == b"SQLDUMP"
    cmd = list(exec_mock.await_args.args)
    env = exec_mock.await_args.kwargs["env"]
    assert env["PGPASSWORD"] == "p@ss/w0rd#"
    assert _cmd_opt(cmd, "-U") == "vpn_user"
    assert _cmd_opt(cmd, "-h") == "db"
    assert _cmd_opt(cmd, "-p") == "5432"
    assert _cmd_opt(cmd, "-d") == "vpnbot"


async def test_query_string_is_not_glued_onto_dbname():
    """?sslmode=... must be stripped; port defaults to 5432 when absent."""
    dsn = "postgresql+asyncpg://u:p@db/vpnbot?sslmode=require&application_name=bot"
    stack, exec_mock = _patch_dsn_and_exec(dsn)
    with stack:
        result = await backup_mod._dump_postgres()
    assert result == b"SQLDUMP"
    cmd = list(exec_mock.await_args.args)
    assert _cmd_opt(cmd, "-d") == "vpnbot"
    assert _cmd_opt(cmd, "-p") == "5432"


async def test_plain_dsn_still_parsed_like_before():
    """No regression on the simple DSN shape the old splitter handled."""
    dsn = "postgresql+asyncpg://user:secret@127.0.0.1:6543/db"
    stack, exec_mock = _patch_dsn_and_exec(dsn)
    with stack:
        result = await backup_mod._dump_postgres()
    assert result == b"SQLDUMP"
    cmd = list(exec_mock.await_args.args)
    env = exec_mock.await_args.kwargs["env"]
    assert env["PGPASSWORD"] == "secret"
    assert _cmd_opt(cmd, "-U") == "user"
    assert _cmd_opt(cmd, "-h") == "127.0.0.1"
    assert _cmd_opt(cmd, "-p") == "6543"
    assert _cmd_opt(cmd, "-d") == "db"


async def test_percent_encoded_username_and_dbname_are_decoded():
    dsn = "postgresql+asyncpg://team%2Bbot:pw@db:5432/vpn%2Dprod"
    stack, exec_mock = _patch_dsn_and_exec(dsn)
    with stack:
        await backup_mod._dump_postgres()
    cmd = list(exec_mock.await_args.args)
    assert _cmd_opt(cmd, "-U") == "team+bot"
    assert _cmd_opt(cmd, "-d") == "vpn-prod"


async def test_unparseable_dsn_returns_none_without_spawning():
    """A DSN with no usable user/dbname fails fast, no subprocess."""
    stack, exec_mock = _patch_dsn_and_exec("not-a-valid-dsn")
    with stack:
        result = await backup_mod._dump_postgres()
    assert result is None
    exec_mock.assert_not_awaited()


async def test_invalid_port_returns_none_without_spawning():
    """urlsplit raises ValueError on a non-numeric port — handled."""
    stack, exec_mock = _patch_dsn_and_exec("postgresql://u:p@db:notaport/vpnbot")
    with stack:
        result = await backup_mod._dump_postgres()
    assert result is None
    exec_mock.assert_not_awaited()


# ─── failure-alert throttling ────────────────────────────────────────────


async def test_first_auto_failure_alerts_all_admins(mock_session, bot):
    repo = _make_repo()
    with _patch_run_backup_internals(repo, pg_dump_result=None):
        await backup_mod.run_backup(mock_session, bot)
    alerted = {call.args[0] for call in bot.send_message.await_args_list}
    assert alerted == ADMIN_IDS
    bot.send_document.assert_not_awaited()


async def test_repeat_auto_failure_within_interval_is_throttled(mock_session, bot):
    repo = _make_repo()
    with _patch_run_backup_internals(repo, pg_dump_result=None):
        await backup_mod.run_backup(mock_session, bot)
        bot.send_message.reset_mock()
        await backup_mod.run_backup(mock_session, bot)  # next 30-min tick
    bot.send_message.assert_not_awaited()


async def test_auto_failure_alerts_again_after_interval(mock_session, bot):
    repo = _make_repo()
    # Simulate the previous alert having happened > interval ago.
    backup_mod._last_failure_notified_monotonic = (
        time.monotonic() - backup_mod._FAILURE_NOTIFY_MIN_INTERVAL_SECONDS - 1
    )
    with _patch_run_backup_internals(repo, pg_dump_result=None):
        await backup_mod.run_backup(mock_session, bot)
    assert bot.send_message.await_count == len(ADMIN_IDS)


async def test_manual_failure_alert_bypasses_throttle(mock_session, bot):
    repo = _make_repo()
    backup_mod._last_failure_notified_monotonic = time.monotonic()  # just alerted
    with _patch_run_backup_internals(repo, pg_dump_result=None):
        await backup_mod.run_backup(mock_session, bot, manual_requester_id=42)
    bot.send_message.assert_awaited_once()
    assert bot.send_message.await_args.args[0] == 42


async def test_forced_failure_alert_bypasses_throttle(mock_session, bot):
    repo = _make_repo()
    backup_mod._last_failure_notified_monotonic = time.monotonic()  # just alerted
    with _patch_run_backup_internals(repo, pg_dump_result=None):
        await backup_mod.run_backup(mock_session, bot, force=True)
    alerted = {call.args[0] for call in bot.send_message.await_args_list}
    assert alerted == ADMIN_IDS


async def test_success_resets_failure_throttle(mock_session, bot):
    """After a successful dump, the next failure episode alerts immediately."""
    repo = _make_repo()
    backup_mod._last_failure_notified_monotonic = time.monotonic()
    with _patch_run_backup_internals(repo, pg_dump_result=b"PGDATA"):
        await backup_mod.run_backup(mock_session, bot)
    assert backup_mod._last_failure_notified_monotonic is None
    bot.send_document.assert_awaited_once()
