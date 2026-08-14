"""
Regression tests for the backup interval gate vs. the dashboard
"Run backup now" trigger (deep-debug finding #6).

Bug: apps/api/routes/dashboard/settings.py called
run_backup(session, bot, manual_requester_id=None), but
manual_requester_id=None is exactly the branch that ENABLES the
interval gate in apps/worker/jobs/backup.py — so "run now" silently
skipped the backup whenever a prior backup ran within the configured
interval, while still returning {"ok": True}.

Fix: run_backup grew an explicit `force: bool = False` parameter that
bypasses ONLY the interval gate (delivery routing for
manual_requester_id=None is unchanged), and the dashboard endpoint
passes force=True.

Mock-based only — no DB, no network.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from apps.api.routes.dashboard import settings as settings_routes
from apps.worker.jobs import backup as backup_mod


CHANNEL_ID = -1001234567890
ADMIN_IDS = {111, 222}


@asynccontextmanager
async def _acquired_lock(*args, **kwargs):
    yield True


def _make_repo(last_run_iso: str | None, interval_hours: int = 6):
    repo = MagicMock()
    repo.get_backup_interval_hours = AsyncMock(return_value=interval_hours)
    repo.get_backup_last_run_iso = AsyncMock(return_value=last_run_iso)
    repo.get_backup_channel_id = AsyncMock(return_value=CHANNEL_ID)
    repo.get_sales_report_chat_id = AsyncMock(return_value=None)
    repo.set_backup_last_run_now = AsyncMock()
    return repo


def _patch_backup_internals(repo):
    """Patch the heavy helpers inside the backup module; return the stack."""
    stack = ExitStack()
    stack.enter_context(patch.object(
        backup_mod, "distributed_lock", _acquired_lock))
    stack.enter_context(patch.object(
        backup_mod, "_write_backup_atomic", MagicMock(return_value="backups/test.tar.gz")))
    stack.enter_context(patch.object(
        backup_mod, "AppSettingsRepository", MagicMock(return_value=repo)))
    dump = stack.enter_context(patch.object(
        backup_mod, "_dump_postgres", AsyncMock(return_value=b"PGDATA")))
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
    return stack, dump


def _recent_iso() -> str:
    """A last_run timestamp well inside the 6h interval."""
    return (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()


def _stale_iso() -> str:
    """A last_run timestamp well past the 6h interval."""
    return (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()


@pytest.fixture
def bot():
    b = AsyncMock()
    b.send_document = AsyncMock()
    b.send_message = AsyncMock()
    return b


async def test_auto_run_within_interval_is_skipped(mock_session, bot):
    """Scheduled path (no manual id, no force) still honors the gate."""
    repo = _make_repo(last_run_iso=_recent_iso())
    stack, dump = _patch_backup_internals(repo)
    with stack:
        await backup_mod.run_backup(mock_session, bot)
    dump.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    repo.set_backup_last_run_now.assert_not_awaited()


async def test_force_bypasses_interval_gate(mock_session, bot):
    """force=True must produce a backup even right after a prior one."""
    repo = _make_repo(last_run_iso=_recent_iso())
    stack, dump = _patch_backup_internals(repo)
    with stack:
        await backup_mod.run_backup(mock_session, bot, force=True)
    dump.assert_awaited_once()
    bot.send_document.assert_awaited_once()


async def test_force_keeps_channel_routing_and_stamps_last_run(mock_session, bot):
    """force=True with manual_requester_id=None still routes to the
    configured backup channel (not admin DMs) and stamps last_run_at."""
    repo = _make_repo(last_run_iso=_recent_iso())
    stack, _ = _patch_backup_internals(repo)
    with stack:
        await backup_mod.run_backup(mock_session, bot, force=True)
    sent_to = bot.send_document.await_args.args[0]
    assert sent_to == CHANNEL_ID
    repo.set_backup_last_run_now.assert_awaited_once()


async def test_manual_requester_unchanged(mock_session, bot):
    """Existing manual path: bypasses the gate, delivers ONLY to the
    requester, and does NOT stamp last_run_at."""
    repo = _make_repo(last_run_iso=_recent_iso())
    stack, dump = _patch_backup_internals(repo)
    with stack:
        await backup_mod.run_backup(mock_session, bot, manual_requester_id=42)
    dump.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.args[0] == 42
    repo.set_backup_last_run_now.assert_not_awaited()


async def test_scheduled_run_fires_when_interval_elapsed(mock_session, bot):
    """Auto path still fires once the interval has passed (no regression)."""
    repo = _make_repo(last_run_iso=_stale_iso())
    stack, dump = _patch_backup_internals(repo)
    with stack:
        await backup_mod.run_backup(mock_session, bot)
    dump.assert_awaited_once()
    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.args[0] == CHANNEL_ID
    repo.set_backup_last_run_now.assert_awaited_once()


@asynccontextmanager
async def _rejected_lock(*args, **kwargs):
    yield False


async def test_distributed_lock_excludes_concurrent_run(mock_session, bot):
    with patch.object(backup_mod, "distributed_lock", _rejected_lock), \
         patch.object(backup_mod, "_run_backup_unlocked", AsyncMock()) as unlocked:
        result = await backup_mod.run_backup(mock_session, bot, force=True)
    assert result.status == "in_progress"
    assert result.success is False
    unlocked.assert_not_awaited()


async def test_local_write_failure_stops_before_upload(mock_session, bot):
    repo = _make_repo(last_run_iso=None)
    stack, _ = _patch_backup_internals(repo)
    with stack, patch.object(
        backup_mod,
        "_write_backup_atomic",
        MagicMock(side_effect=OSError("disk full")),
    ):
        result = await backup_mod.run_backup(mock_session, bot, force=True)
    assert result.status == "local_write_failed"
    assert result.success is False
    bot.send_document.assert_not_awaited()
    repo.set_backup_last_run_now.assert_not_awaited()


async def test_upload_failure_is_reported_and_not_stamped(mock_session, bot):
    repo = _make_repo(last_run_iso=None)
    bot.send_document.side_effect = RuntimeError("Telegram unavailable")
    stack, _ = _patch_backup_internals(repo)
    with stack:
        result = await backup_mod.run_backup(mock_session, bot, force=True)
    assert result.status == "upload_failed"
    assert result.success is False
    repo.set_backup_last_run_now.assert_not_awaited()


async def test_pg_dump_timeout_kills_and_reaps_process():
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", b""))
    proc.kill = MagicMock()

    async def timeout(awaitable, *, timeout):
        assert timeout == 180
        awaitable.close()
        raise asyncio.TimeoutError

    with patch.object(
        backup_mod.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=proc),
    ), patch.object(backup_mod.asyncio, "wait_for", timeout):
        result = await backup_mod._dump_postgres()
    assert result is None
    proc.kill.assert_called_once_with()
    proc.communicate.assert_awaited_once_with()


def test_atomic_write_replaces_final_file_and_leaves_no_temp(tmp_path):
    path = backup_mod._write_backup_atomic(str(tmp_path), "backup.tar.gz", b"new")
    assert path == str(tmp_path / "backup.tar.gz")
    assert (tmp_path / "backup.tar.gz").read_bytes() == b"new"
    assert list(tmp_path.glob("*.tmp")) == []


async def test_dashboard_trigger_returns_truthful_success(mock_session):
    admin = MagicMock()
    bot_instance = MagicMock()
    bot_instance.session.close = AsyncMock()
    result = backup_mod.BackupResult(
        status="delivered",
        success=True,
        delivered=1,
        target_count=1,
        local_path="backups/test.tar.gz",
    )
    run = AsyncMock(return_value=result)
    audit = AsyncMock()
    with patch("aiogram.Bot", return_value=bot_instance), \
         patch("apps.worker.jobs.backup.run_backup", run), \
         patch.object(settings_routes, "_audit", audit):
        response = await settings_routes.trigger_backup_now((admin, mock_session))
    assert response["status"] == "delivered"
    assert response["delivered"] == 1
    audit.assert_awaited_once_with(
        mock_session,
        admin,
        "backup_run_now",
        {"status": "delivered"},
    )
    bot_instance.session.close.assert_awaited_once_with()


async def test_dashboard_trigger_rejects_upload_failure(mock_session):
    admin = MagicMock()
    bot_instance = MagicMock()
    bot_instance.session.close = AsyncMock()
    result = backup_mod.BackupResult(
        status="upload_failed",
        success=False,
        local_path="backups/test.tar.gz",
    )
    audit = AsyncMock()
    with patch("aiogram.Bot", return_value=bot_instance), \
         patch("apps.worker.jobs.backup.run_backup", AsyncMock(return_value=result)), \
         patch.object(settings_routes, "_audit", audit):
        with pytest.raises(HTTPException) as exc_info:
            await settings_routes.trigger_backup_now((admin, mock_session))
    assert exc_info.value.status_code == 502
    assert "upload" in exc_info.value.detail.lower()
    audit.assert_not_awaited()
    bot_instance.session.close.assert_awaited_once_with()
