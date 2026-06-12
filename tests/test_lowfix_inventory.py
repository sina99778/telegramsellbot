"""Regression tests for low-severity inventory/provisioning fixes
(services/provisioning/manager.py).

Covers:
- #87: reserve_plan_sale took SELECT … FOR UPDATE on the PlanInventory row on
  the OUTER session, holding the row lock across the panel network call
  (retries + backoff ≈ a minute on a degraded panel) and the rest of the
  handler — every concurrent buyer of a stock-limited plan serialized on the
  lock while pinning a pooled DB connection. Fixed: _reserve_stock claims the
  unit in its OWN short-lived transaction (committed BEFORE the panel call)
  and _release_stock compensates in another short transaction on failure.
- #88: a panel-create with an UNCERTAIN outcome (mid-flight timeout, or the
  transport retrying POST api/user into a 409 after the first attempt
  committed server-side) skipped the compensating delete because the
  success-only flag (pg_created / xui_call_succeeded) was still False —
  leaving a live, full-quota orphan user on the panel forever. Fixed: the
  compensation now runs whenever the create call was ATTEMPTED; delete is
  safe when nothing was created (PG treats 404 as already-gone, the X-UI
  delete just fails harmlessly and is logged).
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from services.pasarguard.client import PasarGuardRequestError
from services.provisioning.manager import ProvisioningManager
from services.xui.client import XUIRequestError


def _fake_panel_cm(fake_api):
    @asynccontextmanager
    async def _cm(server):
        yield fake_api

    return _cm


def _fake_stock_factory(events: list[str]):
    """Stand-in for AsyncSessionFactory: yields a dedicated stock session whose
    begin() context records when the short transaction opens and commits."""
    stock_session = MagicMock(name="stock_session")

    @asynccontextmanager
    async def _begin():
        events.append("stock_tx_begin")
        yield
        events.append("stock_tx_commit")

    stock_session.begin = _begin

    @asynccontextmanager
    async def _factory():
        yield stock_session

    return _factory, stock_session


# ─── X-UI provisioning fixture (mirrors test_medfix_provisioning) ────────────


def _make_provision_fixture(mock_session):
    plan = MagicMock()
    plan.id = uuid4()
    plan.is_active = True
    plan.volume_bytes = 5 * 1024**3
    plan.effective_ip_limit = MagicMock(return_value=2)

    inbound = MagicMock()
    inbound.id = uuid4()
    inbound.is_active = True
    inbound.xui_inbound_remote_id = 5
    inbound.protocol = "vless"
    server = inbound.server
    server.is_active = True
    server.max_clients = None
    server.panel_type = "xui"  # NOT Marzban-family → native X-UI path
    plan.inbound = inbound

    order = MagicMock()
    order.user_id = uuid4()
    order.status = "paid"

    # scalar order: plan → ready_pool (None)
    mock_session.scalar = AsyncMock(side_effect=[plan, None])
    mock_session.get = AsyncMock(return_value=order)

    manager = ProvisioningManager(mock_session)
    manager._generate_unique_client_identity = AsyncMock(
        return_value=("client-uuid", "u_throwaway", "u_throwaway@tg.local", "abcdef9999999999")
    )
    fake_api = MagicMock()
    fake_api.add_client_to_inbound = AsyncMock()
    fake_api.delete_client = AsyncMock()
    manager._get_xui_client_for_server = _fake_panel_cm(fake_api)
    return manager, plan, order, fake_api


def _provision_patches(events, factory):
    async def _reserve(session, plan_id):
        events.append(("reserve", session))
        return True

    async def _release(session, plan_id):
        events.append(("release", session))

    return (
        patch("services.provisioning.manager.AppSettingsRepository"),
        patch("services.provisioning.manager.build_sub_link", return_value="https://h/sub/abcdef"),
        patch("services.provisioning.manager.build_vless_uri", return_value="vless://x"),
        patch("services.provisioning.manager.AsyncSessionFactory", factory),
        patch("services.provisioning.manager.reserve_plan_sale", side_effect=_reserve),
        patch("services.provisioning.manager.release_plan_sale", side_effect=_release),
    )


async def _run_provision(manager, plan, order, events, factory):
    repo_p, link_p, uri_p, factory_p, reserve_p, release_p = _provision_patches(events, factory)
    with repo_p as repo_cls, link_p, uri_p, factory_p, reserve_p, release_p:
        repo_cls.return_value.get_service_security_settings = AsyncMock(
            return_value=NS(xui_limit_ip=1)
        )
        return await manager.provision_subscription(
            user_id=order.user_id,
            plan_id=plan.id,
            order_id=uuid4(),
            config_name="myname",
        )


# ─── #87: stock reservation must not hold a lock across panel I/O ────────────


@pytest.mark.asyncio
async def test_reserve_runs_in_dedicated_short_tx_committed_before_panel_call(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)
    events: list = []
    factory, stock_session = _fake_stock_factory(events)
    fake_api.add_client_to_inbound = AsyncMock(
        side_effect=lambda *a, **k: events.append("panel_add")
    )

    await _run_provision(manager, plan, order, events, factory)

    # The reservation happened on the DEDICATED session, not the outer one.
    reserve_events = [e for e in events if isinstance(e, tuple) and e[0] == "reserve"]
    assert len(reserve_events) == 1
    assert reserve_events[0][1] is stock_session
    assert reserve_events[0][1] is not mock_session
    # The short transaction committed (lock released) BEFORE the panel call.
    assert events.index("stock_tx_commit") < events.index("panel_add")


@pytest.mark.asyncio
async def test_panel_failure_releases_stock_in_its_own_short_tx(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)
    events: list = []
    factory, stock_session = _fake_stock_factory(events)
    fake_api.add_client_to_inbound = AsyncMock(side_effect=Exception("panel down"))

    with pytest.raises(Exception, match="panel down"):
        await _run_provision(manager, plan, order, events, factory)

    release_events = [e for e in events if isinstance(e, tuple) and e[0] == "release"]
    assert len(release_events) == 1
    assert release_events[0][1] is stock_session  # never the outer session


@pytest.mark.asyncio
async def test_release_stock_swallows_release_errors(mock_session):
    # Compensation must never mask the ORIGINAL provisioning failure.
    manager = ProvisioningManager(mock_session)
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    with (
        patch("services.provisioning.manager.AsyncSessionFactory", factory),
        patch(
            "services.provisioning.manager.release_plan_sale",
            new=AsyncMock(side_effect=Exception("db gone")),
        ),
    ):
        await manager._release_stock(uuid4())  # must not raise


# ─── #88: compensating delete runs on UNCERTAIN create outcomes ───────────────


@pytest.mark.asyncio
async def test_xui_uncertain_timeout_triggers_compensating_delete(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    # Post-batch-2 the X-UI client surfaces a mid-flight timeout on the
    # non-idempotent addClient WITHOUT retrying — the outcome is uncertain.
    fake_api.add_client_to_inbound = AsyncMock(
        side_effect=XUIRequestError("Timed out while calling X-UI endpoint 'addClient'.")
    )

    with pytest.raises(XUIRequestError):
        await _run_provision(manager, plan, order, events, factory)

    # Best-effort cleanup by OUR freshly-generated uuid — no orphan remains.
    fake_api.delete_client.assert_awaited_once_with(
        inbound_id=5, client_id="client-uuid"
    )


@pytest.mark.asyncio
async def test_xui_no_compensation_when_panel_was_never_called(mock_session):
    manager, plan, order, fake_api = _make_provision_fixture(mock_session)
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    # DB flush dies BEFORE the panel call — nothing to clean up panel-side.
    mock_session.flush = AsyncMock(side_effect=Exception("db down"))

    with pytest.raises(Exception, match="db down"):
        await _run_provision(manager, plan, order, events, factory)

    fake_api.add_client_to_inbound.assert_not_awaited()
    fake_api.delete_client.assert_not_awaited()


# ─── #88: PasarGuard/Rebecca path ─────────────────────────────────────────────


class FakeMarzbanClient:
    def __init__(self, *, create_exc=None, pg_user=None):
        self.create_exc = create_exc
        self.pg_user = pg_user
        self.create_calls: list = []
        self.deleted: list = []

    async def create_user_in_bundle(self, **kwargs):
        self.create_calls.append(kwargs)
        if self.create_exc is not None:
            raise self.create_exc
        return self.pg_user

    async def delete_user(self, username):
        self.deleted.append(username)


def _make_pg_fixture(mock_session, fake_client):
    plan = MagicMock()
    plan.id = uuid4()
    plan.volume_bytes = 5 * 1024**3
    plan.duration_days = 30

    order = MagicMock()
    order.id = uuid4()

    inbound = MagicMock()
    inbound.id = uuid4()
    inbound.xui_inbound_remote_id = 7

    server = NS(panel_type="pasarguard", base_url="http://pg:8000", name="pg1")

    manager = ProvisioningManager(mock_session)
    manager._generate_unique_pg_username = AsyncMock(return_value="vpn_deadbeef")
    return manager, plan, order, inbound, server


async def _run_pg_provision(manager, plan, order, inbound, server, fake_client, events, factory):
    async def _reserve(session, plan_id):
        events.append(("reserve", session))
        return True

    async def _release(session, plan_id):
        events.append(("release", session))

    with (
        patch("services.provisioning.manager.marzban_client_for_server", _fake_panel_cm(fake_client)),
        patch("services.provisioning.manager.AsyncSessionFactory", factory),
        patch("services.provisioning.manager.reserve_plan_sale", side_effect=_reserve),
        patch("services.provisioning.manager.release_plan_sale", side_effect=_release),
    ):
        return await manager._provision_pasarguard(
            user_id=uuid4(),
            plan=plan,
            order=order,
            inbound=inbound,
            server=server,
            config_name="vpn",
        )


@pytest.mark.asyncio
async def test_pg_create_retried_into_409_still_compensates_delete(mock_session):
    # The transport retries POST api/user on timeout; if the first attempt
    # committed server-side, the retry surfaces as a 409 uniqueness error
    # while the user is LIVE on the panel. The old success-only flag
    # (pg_created) skipped the delete here — the exact orphan scenario.
    fake_client = FakeMarzbanClient(
        create_exc=PasarGuardRequestError(
            "PasarGuard request to 'api/user' failed with status 409: exists",
            status_code=409,
        )
    )
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    manager, plan, order, inbound, server = _make_pg_fixture(mock_session, fake_client)

    with pytest.raises(PasarGuardRequestError):
        await _run_pg_provision(manager, plan, order, inbound, server, fake_client, events, factory)

    assert fake_client.deleted == ["vpn_deadbeef"]
    # Stock was compensated too, on the dedicated session.
    assert ("release", _stock_session) in events


@pytest.mark.asyncio
async def test_pg_definite_create_then_db_failure_still_deletes(mock_session):
    # Pre-existing behavior must survive: create succeeded, a later DB flush
    # failed → the compensating delete still runs.
    pg_user = MagicMock()
    pg_user.absolute_subscription_url = MagicMock(return_value="https://pg/sub/x")
    pg_user.id = 42
    fake_client = FakeMarzbanClient(pg_user=pg_user)
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    manager, plan, order, inbound, server = _make_pg_fixture(mock_session, fake_client)
    # flush #1 (subscription), #2 (order/xui rows) ok; #3 (sub_link fill) dies.
    mock_session.flush = AsyncMock(side_effect=[None, None, Exception("db boom")])

    with pytest.raises(Exception, match="db boom"):
        await _run_pg_provision(manager, plan, order, inbound, server, fake_client, events, factory)

    assert fake_client.deleted == ["vpn_deadbeef"]


@pytest.mark.asyncio
async def test_pg_no_delete_when_create_was_never_attempted(mock_session):
    # Failure BEFORE the panel call (first DB flush) → the high-entropy
    # username was never sent to the panel; no delete must be issued.
    fake_client = FakeMarzbanClient()
    events: list = []
    factory, _stock_session = _fake_stock_factory(events)
    manager, plan, order, inbound, server = _make_pg_fixture(mock_session, fake_client)
    mock_session.flush = AsyncMock(side_effect=Exception("db down"))

    with pytest.raises(Exception, match="db down"):
        await _run_pg_provision(manager, plan, order, inbound, server, fake_client, events, factory)

    assert fake_client.create_calls == []
    assert fake_client.deleted == []
