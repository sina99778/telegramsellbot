"""Regression tests for PAY-004: uncertain panel writes must not leak clients.

When a panel write's outcome is unknown the provisioning code issues a
compensating delete. If that delete also fails, the enclosing transaction rolls
back and nothing local remembers the identity we sent to the panel — so a client
the panel *did* accept keeps serving traffic at full quota forever while the
user's retry mints a fresh one.

Two halves are covered here:
  * ProvisioningManager._record_possible_orphan — the durable record, written on
    its own committing transaction because the caller's is being rolled back.
  * apps.worker.jobs.orphan_reconcile — the sweep that drains those rows by
    retrying the delete until the panel confirms the client is gone.

The suite is mock-based (no Postgres), so DB effects are asserted on the mapped
objects handed to the session and on the statements the job builds.
"""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from apps.worker.jobs.orphan_reconcile import (
    MAX_DELETE_ATTEMPTS,
    build_pending_orphan_select,
    is_already_gone,
    reconcile_orphaned_panel_clients,
)
from core.database import utcnow
from models.xui import OrphanedPanelClient
from services.provisioning import manager as manager_mod
from services.provisioning.manager import ProvisioningManager
from services.xui.client import XUIRequestError


# ─── the durable record ──────────────────────────────────────────────────────


def _fake_orphan_factory(events: list[str]):
    """Stand-in for AsyncSessionFactory that records the transaction boundaries.

    _record_possible_orphan must not write on the caller's session, so the fake
    yields a dedicated one and notes when its short transaction opens/commits.
    """
    orphan_session = MagicMock(name="orphan_session")
    added: list[object] = []

    @asynccontextmanager
    async def _begin():
        events.append("orphan_tx_begin")
        yield
        events.append("orphan_tx_commit")

    def _add(obj: object) -> None:
        events.append("orphan_add")
        added.append(obj)

    orphan_session.begin = _begin
    orphan_session.add = _add

    @asynccontextmanager
    async def _factory():
        yield orphan_session

    return _factory, orphan_session, added


def _server(name: str = "de-1") -> NS:
    return NS(id=uuid4(), name=name, panel_type="sanaei_xui")


async def test_orphan_row_is_written_on_its_own_committed_transaction(mock_session):
    events: list[str] = []
    factory, orphan_session, added = _fake_orphan_factory(events)
    manager = ProvisioningManager(mock_session)
    server = _server()

    with patch("services.provisioning.manager.AsyncSessionFactory", factory):
        await manager._record_possible_orphan(
            server=server,
            remote_client_id="client-uuid-1",
            inbound_remote_id=42,
            email="cfg@bot",
            origin="provisioning",
            error=RuntimeError("panel timed out"),
        )

    # Committed on a session of its own: the caller's transaction is being
    # rolled back by the failure we are compensating for.
    assert events == ["orphan_tx_begin", "orphan_add", "orphan_tx_commit"]
    assert orphan_session is not mock_session
    mock_session.add.assert_not_called()

    (row,) = added
    assert isinstance(row, OrphanedPanelClient)
    assert row.server_id == server.id
    assert row.remote_client_id == "client-uuid-1"
    assert row.inbound_remote_id == 42
    assert row.panel_kind == "xui"
    assert row.email == "cfg@bot"
    assert row.origin == "provisioning"
    assert "panel timed out" in row.last_error


async def test_orphan_row_is_normalised_to_fit_its_columns(mock_session):
    """A panel error can be arbitrarily long; an oversized INSERT would fail and
    lose exactly the row we need."""
    events: list[str] = []
    factory, _, added = _fake_orphan_factory(events)
    manager = ProvisioningManager(mock_session)

    with patch("services.provisioning.manager.AsyncSessionFactory", factory):
        await manager._record_possible_orphan(
            server=_server(),
            remote_client_id="u" * 400,
            panel_kind="  PasarGuard  ",
            email="e" * 400,
            origin="o" * 100,
            error="x" * 5000,
        )

    (row,) = added
    assert row.panel_kind == "pasarguard"
    assert len(row.remote_client_id) == 128
    assert len(row.email) == 128
    assert len(row.origin) == 32
    assert len(row.last_error) == 2000


async def test_failing_to_record_never_masks_the_original_error(mock_session):
    """The helper runs inside an `except` block that is about to re-raise: if it
    threw, the user would see a DB error instead of the real provisioning one."""
    manager = ProvisioningManager(mock_session)

    def _broken_factory():
        raise ConnectionError("db unreachable")

    with patch("services.provisioning.manager.AsyncSessionFactory", _broken_factory):
        assert (
            await manager._record_possible_orphan(
                server=_server(), remote_client_id="client-uuid-1"
            )
            is None
        )


def test_every_compensating_delete_failure_records_an_orphan():
    """All five cleanup sites must persist the identity, each with its own origin
    so an admin can tell a provisioning orphan from a migration one."""
    source = inspect.getsource(manager_mod)
    assert source.count("await self._record_possible_orphan(") >= 5
    for origin in ("provisioning", "migration", "migration_old_panel", "imported_migration"):
        assert f'origin="{origin}"' in source


# ─── the sweep: query and classification ─────────────────────────────────────


def test_pending_query_only_returns_rows_still_worth_an_attempt():
    sql = str(build_pending_orphan_select().compile(compile_kwargs={"literal_binds": True}))
    assert "orphaned_panel_clients.resolved_at IS NULL" in sql
    assert f"orphaned_panel_clients.attempts < {MAX_DELETE_ATTEMPTS}" in sql
    # Oldest first, bounded work per run — one panel round-trip per row.
    assert "ORDER BY orphaned_panel_clients.created_at ASC" in sql
    assert "LIMIT" in sql


def test_already_gone_recognises_a_404_status():
    assert is_already_gone(XUIRequestError("nope", status_code=404)) is True


def test_already_gone_recognises_the_panels_free_text_rejection():
    """Sanaei's delClient answers `success: false` with a message and no status
    code, so the wording is the only signal available."""
    assert is_already_gone(XUIRequestError("Client not found.")) is True
    assert is_already_gone(RuntimeError("user does not exist")) is True


def test_already_gone_is_false_for_failures_that_prove_nothing():
    assert is_already_gone(XUIRequestError("bad gateway", status_code=502)) is False
    assert is_already_gone(TimeoutError("read timeout")) is False


# ─── the sweep: behaviour ────────────────────────────────────────────────────


def _orphan_row(
    *,
    panel_kind: str = "xui",
    inbound_remote_id: int | None = 42,
    remote_client_id: str = "client-uuid-1",
    attempts: int = 0,
    origin: str = "provisioning",
) -> OrphanedPanelClient:
    return OrphanedPanelClient(
        id=uuid4(),
        server_id=uuid4(),
        panel_kind=panel_kind,
        inbound_remote_id=inbound_remote_id,
        remote_client_id=remote_client_id,
        origin=origin,
        attempts=attempts,
    )


def _session_cm(session):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _read_session(ids) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(ids)
    session.execute = AsyncMock(return_value=result)
    return session


def _row_session(row, server) -> AsyncMock:
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=row)
    session.get = AsyncMock(return_value=server)
    return session


def _factory(sessions):
    """The job opens one read session plus one per row, so the count depends on
    the data — hand them out in order."""
    iterator = iter(sessions)

    def factory():
        return _session_cm(next(iterator))

    return factory


def _fake_panel(method_name: str, behaviour=None):
    api = MagicMock()
    setattr(api, method_name, behaviour or AsyncMock())

    @asynccontextmanager
    async def _cm(server):
        yield api

    return _cm, api


async def _run_sweep(pairs, *, xui_delete=None, marzban_delete=None, sessions=None):
    """Run the sweep over (row, server) pairs against fake panels."""
    if sessions is None:
        sessions = [_read_session([row.id for row, _ in pairs])]
        sessions += [_row_session(row, server) for row, server in pairs]
    xui_cm, xui_api = _fake_panel("delete_client", xui_delete)
    marzban_cm, marzban_api = _fake_panel("delete_user", marzban_delete)

    with patch("apps.worker.jobs.orphan_reconcile.AsyncSessionFactory", _factory(sessions)), \
         patch("apps.worker.jobs.orphan_reconcile.create_xui_client_for_server", xui_cm), \
         patch("apps.worker.jobs.orphan_reconcile.marzban_client_for_server", marzban_cm):
        resolved = await reconcile_orphaned_panel_clients()

    return NS(resolved=resolved, xui=xui_api, marzban=marzban_api, sessions=sessions)


async def test_sweep_is_a_noop_when_nothing_is_pending():
    """The common case: one indexed SELECT that returns nothing, no row sessions
    opened and no panel touched."""
    outcome = await _run_sweep([], sessions=[_read_session([])])

    assert outcome.resolved == 0
    outcome.xui.delete_client.assert_not_awaited()


async def test_successful_delete_closes_the_row():
    server = _server()
    row = _orphan_row()
    row.server_id = server.id

    outcome = await _run_sweep([(row, server)])

    outcome.xui.delete_client.assert_awaited_once_with(
        inbound_id=42, client_id="client-uuid-1"
    )
    assert outcome.resolved == 1
    assert row.resolution == "deleted"
    assert row.resolved_at is not None
    assert row.last_error is None
    assert row.attempts == 1
    outcome.sessions[1].commit.assert_awaited_once()


async def test_client_that_was_never_created_is_closed_as_already_gone():
    """A "not found" answer proves the uncertain write never landed — the happy
    outcome, not an error to retry."""
    server = _server()
    row = _orphan_row()
    row.server_id = server.id

    outcome = await _run_sweep(
        [(row, server)],
        xui_delete=AsyncMock(side_effect=XUIRequestError("Client not found.")),
    )

    assert outcome.resolved == 1
    assert row.resolution == "already_gone"
    assert row.resolved_at is not None
    assert row.last_error is None


async def test_hard_failure_keeps_the_row_open_and_records_the_attempt():
    server = _server()
    row = _orphan_row()
    row.server_id = server.id

    outcome = await _run_sweep(
        [(row, server)],
        xui_delete=AsyncMock(side_effect=XUIRequestError("panel unreachable")),
    )

    assert outcome.resolved == 0
    assert row.resolved_at is None
    assert row.resolution is None
    assert row.attempts == 1
    assert row.last_attempt_at is not None
    assert "panel unreachable" in row.last_error
    # Bookkeeping is still committed, otherwise attempts could never grow and a
    # dead panel would be retried forever.
    outcome.sessions[1].commit.assert_awaited_once()


async def test_a_permanently_dead_panel_is_escalated_instead_of_retried_forever():
    server = _server()
    row = _orphan_row(attempts=MAX_DELETE_ATTEMPTS - 1)
    row.server_id = server.id

    outcome = await _run_sweep(
        [(row, server)],
        xui_delete=AsyncMock(side_effect=XUIRequestError("panel unreachable")),
    )

    assert outcome.resolved == 0
    assert row.attempts == MAX_DELETE_ATTEMPTS
    # attempts < MAX is the query filter, so this row is now out of scope and an
    # admin has been told once via the log.
    assert row.resolved_at is None


async def test_marzban_family_rows_use_the_user_delete_primitive():
    """PasarGuard/Rebecca address clients by username and have no inbound, so the
    X-UI primitive would be wrong (and unusable) here."""
    server = NS(id=uuid4(), name="pg-1", panel_type="pasarguard")
    row = _orphan_row(
        panel_kind="pasarguard", inbound_remote_id=None, remote_client_id="cfg_ab12cd34"
    )
    row.server_id = server.id

    outcome = await _run_sweep([(row, server)])

    outcome.marzban.delete_user.assert_awaited_once_with("cfg_ab12cd34")
    outcome.xui.delete_client.assert_not_awaited()
    assert row.resolution == "deleted"


async def test_xui_row_without_an_inbound_is_never_deleted_blindly():
    """Without an inbound id there is nothing to address; guessing one could hit
    a different inbound's client."""
    server = _server()
    row = _orphan_row(inbound_remote_id=None)
    row.server_id = server.id

    outcome = await _run_sweep([(row, server)])

    outcome.xui.delete_client.assert_not_awaited()
    assert row.resolved_at is None
    assert "inbound" in row.last_error


async def test_missing_server_is_skipped_without_burning_an_attempt():
    """The FK cascades, so this only races an admin deleting the server — with no
    base_url or credentials left the client is simply unreachable."""
    row = _orphan_row()

    outcome = await _run_sweep([], sessions=[_read_session([row.id]), _row_session(row, None)])

    assert outcome.resolved == 0
    assert row.attempts == 0
    outcome.xui.delete_client.assert_not_awaited()


async def test_each_row_is_relocked_with_skip_locked():
    """Candidate ids are read without a lock, then each row is re-locked in its
    own session so two workers can never delete the same client twice."""
    server = _server()
    row = _orphan_row()
    row.server_id = server.id

    outcome = await _run_sweep([(row, server)])

    list_sql = str(outcome.sessions[0].execute.await_args.args[0])
    assert "FOR UPDATE" not in list_sql
    locked = outcome.sessions[1].scalar.await_args.args[0]
    assert locked._for_update_arg is not None
    assert locked._for_update_arg.skip_locked is True


async def test_one_broken_row_cannot_abort_the_sweep():
    """Per-row sessions: an error on one orphan must not poison the rest with
    PendingRollbackError."""
    server = _server()
    good = _orphan_row(remote_client_id="good-uuid")
    good.server_id = server.id
    broken = _orphan_row(remote_client_id="broken-uuid")

    broken_session = AsyncMock()
    broken_session.scalar = AsyncMock(side_effect=ConnectionError("lock wait timeout"))
    sessions = [
        _read_session([broken.id, good.id]),
        broken_session,
        _row_session(good, server),
    ]

    outcome = await _run_sweep([], sessions=sessions)

    broken_session.rollback.assert_awaited_once()
    assert outcome.resolved == 1
    assert good.resolution == "deleted"
    outcome.xui.delete_client.assert_awaited_once_with(inbound_id=42, client_id="good-uuid")


async def test_already_resolved_rows_are_left_alone():
    """The unlocked read can race a concurrent worker that already closed the
    row; the post-lock re-check is what makes the delete exactly-once."""
    server = _server()
    row = _orphan_row()
    row.server_id = server.id
    row.resolved_at = utcnow()
    row.resolution = "deleted"

    outcome = await _run_sweep([(row, server)])

    assert outcome.resolved == 0
    outcome.xui.delete_client.assert_not_awaited()
