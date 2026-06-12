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

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.worker.jobs import backup as backup_mod


CHANNEL_ID = -1001234567890
ADMIN_IDS = {111, 222}


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
