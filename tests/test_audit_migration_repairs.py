from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from schemas.internal.xui import XUIInbound
from services.provisioning import manager as mod


@pytest.fixture
def migration_case(mock_session):
    server = NS(id=uuid4(), credentials=NS())
    sub = NS(id=uuid4(), source=None, legacy_remark="old-name", volume_bytes=1000,
             lifetime_used_bytes=0, migration_usage_reconciled=False,
             migration_usage_review_reason=None,
             panel_sync_pending=False, status="active", order_id=None)
    row = NS(sub_id=sub.id, legacy_remark="old-name", new_inbound=2,
             email="new-name", lifetime_used_bytes=0, ends_at=None, sub_link=None,
             xc_sub_link=None, plan_ip_limit=1, client_uuid="new-client",
             xui_client_remote_id="new-client", username="fixture", is_active=True)
    statements = []
    async def execute(statement):
        statements.append(statement)
        result = MagicMock()
        result.all.return_value = [row]
        result.scalar_one_or_none.return_value = sub
        return result
    mock_session.execute.side_effect = execute
    mock_session.scalar.side_effect = lambda stmt: server if "xui_servers" in str(stmt) else None
    api = NS(get_inbounds=AsyncMock(return_value=[XUIInbound(
        id=1, settings={"clients": []},
        clientStats=[{"email": "old-name", "total": 1000, "up": 200, "down": 0}],
    )]), patch_client=AsyncMock())
    manager = mod.ProvisioningManager(mock_session)
    @asynccontextmanager
    async def factory(server):
        yield api
    async def run(**kwargs):
        repo = MagicMock(get_service_security_settings=AsyncMock(return_value=NS(xui_limit_ip=1)))
        with patch.object(mod, "is_marzban_family", return_value=False), \
             patch.object(mod, "AppSettingsRepository", return_value=repo), \
             patch.object(manager, "_get_xui_client_for_server", factory):
            return await manager.reconcile_migrated_usage_for_server(server, **kwargs)
    return NS(sub=sub, row=row, api=api, session=mock_session, statements=statements, run=run)


@pytest.mark.asyncio
async def test_pending_paid_renewal_survives_migration_backfill(migration_case):
    c = migration_case
    c.sub.volume_bytes = 2000
    c.sub.panel_sync_pending = True
    result = await c.run()
    assert result["fixed"] == 0
    assert result["details"][0]["result"] == "pending_sync"
    assert c.sub.volume_bytes == 2000
    c.api.patch_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_untouched_migration_queues_versioned_desired_state(migration_case):
    c = migration_case
    result = await c.run()
    assert result["fixed"] == 1
    assert c.sub.volume_bytes == 800
    assert c.sub.lifetime_used_bytes == 200
    assert c.sub.migration_usage_reconciled is True
    assert c.sub.panel_sync_pending is True
    assert c.session.flush.await_count == 1
    assert all(stmt.is_select for stmt in c.statements)
    locked = c.statements[-1]
    assert "FOR UPDATE OF subscriptions" in str(locked.compile(dialect=postgresql.dialect()))
    assert locked.get_execution_options()["populate_existing"] is True
    c.api.patch_client.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("volume,lifetime", [(2000, 0), (0, 0), (800, 200), (1000, 200)])
async def test_unknown_or_changed_entitlement_requires_review(migration_case, volume, lifetime):
    c = migration_case
    c.sub.volume_bytes, c.sub.lifetime_used_bytes = volume, lifetime
    result = await c.run(force=True)
    assert result["details"][0]["result"] == "review_required"
    assert result["fixed"] == 0
    assert c.sub.volume_bytes == volume
    assert c.sub.migration_usage_reconciled is False
    assert c.sub.migration_usage_review_reason == "entitlement_baseline_changed"
    c.api.patch_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_volume_plan_renewal_requires_review(migration_case):
    c = migration_case
    c.session.scalar.side_effect = lambda stmt: NS(id=uuid4(), credentials=NS()) if "xui_servers" in str(stmt) else uuid4()
    result = await c.run()
    assert result["details"][0]["result"] == "review_required"
    assert c.sub.volume_bytes == 1000


@pytest.mark.asyncio
async def test_force_cannot_subtract_twice(migration_case):
    c = migration_case
    c.sub.migration_usage_reconciled = True
    result = await c.run(force=True)
    assert result["details"][0]["result"] == "already_reconciled"
    assert c.sub.volume_bytes == 1000


@pytest.mark.asyncio
async def test_dry_run_validates_safety_and_does_not_queue(migration_case):
    c = migration_case
    result = await c.run(dry_run=True)
    assert result["details"][0]["result"] == "would_fix"
    assert c.sub.volume_bytes == 1000
    assert c.sub.panel_sync_pending is False
    c.session.flush.assert_not_awaited()
    c.api.patch_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_renewal_committed_during_panel_fetch_is_rechecked(migration_case):
    c = migration_case
    inbounds = c.api.get_inbounds.return_value
    async def get_inbounds():
        # Candidate snapshot was taken before another session committed renewal.
        c.sub.volume_bytes = 2000
        c.sub.panel_sync_pending = True
        return inbounds
    c.api.get_inbounds.side_effect = get_inbounds
    result = await c.run()
    assert result["details"][0]["result"] == "pending_sync"
    assert c.sub.volume_bytes == 2000
    c.session.flush.assert_not_awaited()
    c.api.patch_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_unlimited_never_becomes_a_finite_correction(migration_case):
    c = migration_case
    c.api.get_inbounds.return_value[0].client_stats[0]["total"] = 0
    result = await c.run()
    assert result["details"][0]["result"] == "old_unlimited"
    assert c.sub.volume_bytes == 1000
    c.session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_depleted_legacy_quota_is_never_unlimited(migration_case):
    c = migration_case
    c.api.get_inbounds.return_value[0].client_stats[0]["up"] = 1200
    result = await c.run()
    assert result["fixed"] == 1
    assert c.sub.volume_bytes == 1
    assert c.sub.lifetime_used_bytes == 1200
    assert c.sub.panel_sync_pending is True


@pytest.mark.asyncio
async def test_dry_run_does_not_persist_review_marker(migration_case):
    c = migration_case
    c.sub.volume_bytes = 2000
    result = await c.run(dry_run=True)
    assert result["details"][0]["result"] == "review_required"
    assert c.sub.migration_usage_review_reason is None
    c.session.flush.assert_not_awaited()
