"""
Regression tests for finding 38 (apps/bot/handlers/admin/recovery.py):

_run_fixvol(apply=True) isolates per-server failures by committing per server
and rolling back the failed one. But Session.rollback() expires EVERY ORM
object loaded in the session (regardless of expire_on_commit=False, which only
affects commit), so iterating pre-loaded XUIServerRecord instances after a
rollback lazy-refreshes an expired object and raises MissingGreenlet — killing
all remaining servers, the exact cross-server isolation the loop exists for.

The fix: snapshot plain (id, name) column pairs before the loop and re-SELECT
each server fresh inside the per-iteration try, so no expired instance is ever
touched after a rollback.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from apps.bot.handlers.admin.recovery import _run_fixvol


def _rows_result(rows):
    """Mock result for the (id, name) snapshot query."""
    res = MagicMock()
    res.all.return_value = rows
    return res


def _entity_result(obj):
    """Mock result for the per-iteration full-record reload."""
    res = MagicMock()
    res.scalars.return_value.first.return_value = obj
    return res


def _ok_res(**overrides):
    base = {
        "checked": 3, "fixed": 2, "no_data": 1, "skipped": 0,
        "details": [{"result": "fixed", "remark": "cfg-1"}],
        "panel_emails": ["a@panel"],
    }
    base.update(overrides)
    return base


@pytest.fixture
def mock_manager():
    with patch("services.provisioning.manager.ProvisioningManager") as manager_cls:
        manager = manager_cls.return_value
        manager.reconcile_migrated_usage_for_server = AsyncMock()
        yield manager


class TestFixvolRollbackIsolation:
    async def test_failed_server_rollback_does_not_kill_remaining_servers(
        self, mock_session, mock_manager,
    ):
        """Apply mode: server 1 errors (rollback), server 2 must still be
        processed on a FRESHLY re-selected instance — not the pre-rollback one."""
        id1, id2 = uuid4(), uuid4()
        fresh_server_2 = MagicMock(name="fresh_server_2")
        mock_session.execute = AsyncMock(side_effect=[
            _rows_result([(id1, "alpha"), (id2, "beta")]),  # snapshot
            _entity_result(MagicMock(name="server_1")),     # reload server 1
            _entity_result(fresh_server_2),                 # reload server 2 (post-rollback)
        ])
        mock_session.rollback = AsyncMock()
        mock_manager.reconcile_migrated_usage_for_server.side_effect = [
            RuntimeError("panel down"),
            _ok_res(),
        ]

        agg = await _run_fixvol(mock_session, apply=True)

        # Failure isolated: error recorded with the snapshotted name…
        assert agg["errors"] == ["alpha: RuntimeError: panel down"]
        mock_session.rollback.assert_awaited_once()
        # …and the second server was still processed and committed.
        assert mock_manager.reconcile_migrated_usage_for_server.await_count == 2
        second_call_server = (
            mock_manager.reconcile_migrated_usage_for_server.await_args_list[1].args[0]
        )
        assert second_call_server is fresh_server_2
        mock_session.commit.assert_awaited_once()
        assert agg["checked"] == 3
        assert agg["fixed"] == 2

    async def test_error_name_comes_from_snapshot_not_orm_instance(
        self, mock_session, mock_manager,
    ):
        """The error line must use the plain snapshotted name; reading .name
        off the (now expired) ORM instance would raise MissingGreenlet."""
        id1 = uuid4()

        class ExplodingName:
            """Mimics an expired AsyncSession instance after rollback."""
            @property
            def name(self):
                raise AssertionError(
                    "ORM .name accessed after rollback (MissingGreenlet in prod)"
                )

        mock_session.execute = AsyncMock(side_effect=[
            _rows_result([(id1, "gamma")]),
            _entity_result(ExplodingName()),
        ])
        mock_session.rollback = AsyncMock()
        mock_manager.reconcile_migrated_usage_for_server.side_effect = (
            RuntimeError("boom")
        )

        agg = await _run_fixvol(mock_session, apply=True)

        assert agg["errors"] == ["gamma: RuntimeError: boom"]

    async def test_server_deleted_between_snapshot_and_reload_is_skipped(
        self, mock_session, mock_manager,
    ):
        """Reload returning None (server deleted mid-run) skips cleanly:
        no reconcile call, no error, next server still handled."""
        id1, id2 = uuid4(), uuid4()
        server_2 = MagicMock(name="server_2")
        mock_session.execute = AsyncMock(side_effect=[
            _rows_result([(id1, "gone"), (id2, "alive")]),
            _entity_result(None),       # server 1 vanished
            _entity_result(server_2),   # server 2 fine
        ])
        mock_manager.reconcile_migrated_usage_for_server.return_value = _ok_res()

        agg = await _run_fixvol(mock_session, apply=True)

        assert agg["errors"] == []
        mock_manager.reconcile_migrated_usage_for_server.assert_awaited_once()
        assert (
            mock_manager.reconcile_migrated_usage_for_server.await_args.args[0]
            is server_2
        )

    async def test_dry_run_error_no_rollback_no_commit_still_continues(
        self, mock_session, mock_manager,
    ):
        """Dry-run mode never commits/rolls back; an error on server 1 is
        recorded and server 2 is still checked."""
        id1, id2 = uuid4(), uuid4()
        mock_session.execute = AsyncMock(side_effect=[
            _rows_result([(id1, "alpha"), (id2, "beta")]),
            _entity_result(MagicMock(name="server_1")),
            _entity_result(MagicMock(name="server_2")),
        ])
        mock_session.rollback = AsyncMock()
        mock_manager.reconcile_migrated_usage_for_server.side_effect = [
            RuntimeError("panel down"),
            _ok_res(),
        ]

        agg = await _run_fixvol(mock_session, apply=False)

        mock_session.rollback.assert_not_awaited()
        mock_session.commit.assert_not_awaited()
        assert agg["errors"] == ["alpha: RuntimeError: panel down"]
        assert agg["checked"] == 3

    async def test_snapshot_query_selects_plain_columns_only(
        self, mock_session, mock_manager,
    ):
        """The up-front query must select scalar (id, name) columns — plain
        Python values immune to rollback expiry — not full ORM entities."""
        mock_session.execute = AsyncMock(side_effect=[_rows_result([])])

        await _run_fixvol(mock_session, apply=True)

        stmt = mock_session.execute.await_args_list[0].args[0]
        cols = list(stmt.selected_columns)
        assert len(cols) == 2
        assert {c.key for c in cols} == {"id", "name"}
