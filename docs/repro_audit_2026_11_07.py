"""Historical audit evidence from before the November 7 repairs.

These defect assertions are intentionally not acceptance tests for repaired code.
Use tests/test_audit_*_repairs.py and tests/test_upgrade_postgres.py instead.

Run explicitly: python -m pytest docs/repro_audit_2026_11_07.py -q
All external IO is replaced. No Telegram, panel, Redis or DB connections.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql


@pytest.fixture
def session():
    s = AsyncMock()
    s.add = MagicMock()
    s.no_autoflush = MagicMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock()
    cm.__aexit__ = AsyncMock(return_value=False)
    s.begin_nested = MagicMock(return_value=cm)
    return s


@pytest.mark.asyncio
async def test_signed_nowpayments_callback_processes_other_provider_invoice(session):
    from apps.api.routes.webhooks import nowpayments as route

    payment = NS(id=uuid4(), user_id=uuid4(), provider="manual_crypto",
                 provider_payment_id="12345678", payment_status="waiting_hash",
                 kind="wallet_topup", price_amount=Decimal("1000"),
                 actually_paid=None, callback_payload={})
    statements = []

    async def lookup(statement):
        compiled = str(statement.compile(dialect=postgresql.dialect()))
        statements.append(compiled)
        # Model a manual invoice matching the numeric hint, with no NOW row
        # matching yet. A provider predicate would prevent this match.
        where = compiled.split("WHERE", 1)[1]
        return None if "payments.provider =" in where else payment

    session.scalar.side_effect = lookup
    secret = "audit-fixture-only"
    payload = {"payment_id": 12345678, "payment_status": "finished", "price_amount": 1}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    request = NS(body=AsyncMock(return_value=body), headers={"x-nowpayments-sig": signature})
    repo = MagicMock(get_gateway_settings=AsyncMock(return_value=NS(nowpayments_ipn_secret=secret)))
    credit = AsyncMock(return_value=True)
    with patch("repositories.settings.AppSettingsRepository", return_value=repo), \
         patch.object(route, "process_successful_payment", credit):
        result = await route.handle_nowpayments_ipn(request, session)
    assert result == {"status": "processed"}
    assert credit.call_args.kwargs["payment"].provider == "manual_crypto"
    assert credit.call_args.kwargs["amount_to_credit"] == Decimal("1000")
    assert "payments.provider =" not in statements[0].split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_unlimited_marzban_update_omits_both_limits():
    from services import renewal

    sent = []
    remote = {"expire": 2000000000, "data_limit": 1000}

    async def modify(username, payload):
        wire = payload.to_payload()
        sent.append(wire)
        remote.update(wire)  # an omitted field retains its previous value

    @asynccontextmanager
    async def client(server):
        yield NS(modify_user=modify)

    sub = NS(status="active", ends_at=None, volume_bytes=0)
    record = NS(inbound=NS(server=NS()), panel_username="fixture", username="fixture")
    with patch.object(renewal, "marzban_client_for_server", client):
        await renewal._sync_pasarguard_limits(sub, record)
    assert sent == [{"status": "active"}]
    assert remote["expire"] == 2000000000 and remote["data_limit"] == 1000
    assert record.is_active is True


@pytest.mark.asyncio
async def test_ban_between_eligibility_check_and_row_lock_accepts_renewal(session):
    from services import renewal

    sub = NS(id=uuid4(), status="active", ends_at=datetime.now(timezone.utc) + timedelta(days=5),
             volume_bytes=1000, used_bytes=0, plan_id=None, panel_sync_pending=False)

    async def refresh(obj, **kwargs):
        # The admin's ban committed after the caller loaded the subscription;
        # its disable outbox has already been delivered before this lock.
        obj.status = "disabled"

    session.refresh.side_effect = refresh
    session.scalar.return_value = NS(inbound=NS(server=NS(credentials=NS())),
                                     xui_client_remote_id="fixture", client_uuid="fixture")
    with patch.object(renewal, "record_is_marzban_family", return_value=False):
        await renewal.apply_renewal(session=session, subscription=sub, renew_type="time", amount=30)
    assert sub.status == "disabled"
    assert sub.panel_sync_pending is True
    assert sub.ends_at > datetime.now(timezone.utc) + timedelta(days=34)


@pytest.mark.asyncio
async def test_panel_outbox_backoff_rows_hide_due_row(session):
    from apps.worker.jobs import panel_sync as job

    now = datetime.now(timezone.utc)
    sleeping = [NS(id=uuid4(), panel_sync_attempted_at=now - timedelta(minutes=2),
                   panel_sync_attempts=7) for _ in range(50)]
    due = NS(id=uuid4(), panel_sync_attempted_at=now - timedelta(seconds=70),
             panel_sync_attempts=1)
    rows = sleeping + [due]
    picked = []

    async def select_ids(statement):
        compiled = str(statement.compile(dialect=postgresql.dialect(),
                                         compile_kwargs={"literal_binds": True}))
        # Reproduce the production ordering/limit; verify there is no due-time
        # predicate in SQL before that limit is applied.
        where = compiled.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
        assert "panel_sync_attempted_at" not in where and "LIMIT 50" in compiled
        result = MagicMock()
        result.all.return_value = [r.id for r in sorted(rows, key=lambda r: r.panel_sync_attempted_at)[:50]]
        return result

    async def get_row(statement):
        pid = statement.compile().params["id_1"]
        picked.append(pid)
        return next(r for r in rows if r.id == pid)

    session.scalars.side_effect = select_ids
    session.scalar.side_effect = get_row

    @asynccontextmanager
    async def factory():
        yield session

    with patch.object(job, "AsyncSessionFactory", factory):
        await job.sync_pending_panel_states()
        await job.sync_pending_panel_states()
    assert due.id not in picked and len(picked) == 100
    assert due.panel_sync_attempted_at + timedelta(seconds=60) < now


@pytest.mark.asyncio
async def test_transfer_changes_existing_panel_limits(session):
    from apps.bot.handlers.user import transfer
    from services.xui.client import SanaeiXUIClient, XUIClientConfig
    from pydantic import SecretStr

    old = {"id": "old", "email": "fixture", "subId": "old-sub", "flow": "keep",
           "totalGB": 9999, "expiryTime": 2000000000000, "limitIp": 7, "enable": True}
    current = dict(old)
    writes = []

    def respond(request):
        if request.url.path == "/panel/api/inbounds/list":
            return httpx.Response(200, json={"success": True, "obj": [
                {"id": 1, "protocol": "vless", "settings": {"clients": [current]}}]})
        if "/updateClient/" in request.url.path:
            current.update(json.loads(json.loads(request.content)["settings"])["clients"][0])
            writes.append(dict(current))
        return httpx.Response(200, json={"success": True})

    server = NS(health_status="healthy", is_active=True, credentials=NS())
    record = NS(panel_kind=None, inbound=NS(xui_inbound_remote_id=1, server=server),
                client_uuid="old", xui_client_remote_id="old", email="fixture", username="fixture",
                is_active=True, sub_link="https://sub.invalid/old-sub")
    sub = NS(id=uuid4(), xui_client=record, ends_at=None, volume_bytes=1000, sub_link=record.sub_link)
    repo = MagicMock(get_service_security_settings=AsyncMock(return_value=NS(xui_limit_ip=1)))
    async with httpx.AsyncClient(base_url="https://panel.invalid/", transport=httpx.MockTransport(respond)) as http:
        client = SanaeiXUIClient(XUIClientConfig("https://panel.invalid", "fixture", SecretStr("fixture")), http_client=http)

        @asynccontextmanager
        async def factory(server):
            yield client

        with patch.object(transfer, "AppSettingsRepository", return_value=repo), \
             patch("services.xui.runtime.ensure_inbound_server_loaded", return_value=server), \
             patch("services.xui.runtime.build_sub_link", return_value="https://sub.invalid/new"), \
             patch("services.xui.runtime.create_xui_client_for_server", factory):
            assert await transfer._rotate_config_identity_for_transfer(session, sub)
    assert len(writes) == 1
    assert writes[0]["limitIp"] == 1 and writes[0]["totalGB"] == 1000
    assert writes[0]["expiryTime"] == 0
    assert writes[0]["flow"] == "keep"  # generic merge works; caller overrides limits


@pytest.mark.asyncio
async def test_migration_backfill_overwrites_paid_renewal_target(session):
    from services.provisioning import manager as mod
    from schemas.internal.xui import XUIInbound

    server = NS(id=uuid4(), credentials=NS())
    row = NS(sub_id=uuid4(), legacy_remark="old-name", lifetime_used_bytes=0,
             ends_at=datetime.now(timezone.utc) + timedelta(days=30),
             sub_link="https://sub.invalid/new", client_uuid="new-client",
             xui_client_remote_id="new-client", email="new-name", username="fixture",
             is_active=True, xc_sub_link=None, new_inbound=2, plan_ip_limit=1)
    # Paid renewal has already committed volume=2000 + pending outbox.
    committed = {"volume_bytes": 2000, "panel_sync_pending": True, "state_version": 5}
    updates = []

    async def execute(statement):
        if statement.is_select:
            compiled = str(statement.compile(dialect=postgresql.dialect()))
            where = compiled.split("WHERE", 1)[1]
            assert "panel_sync_pending" not in where
            result = MagicMock()
            result.all.return_value = [row]
            return result
        compiled = statement.compile(dialect=postgresql.dialect())
        updates.append(str(compiled))
        committed["volume_bytes"] = compiled.params["volume_bytes"]
        return MagicMock()

    session.scalar.return_value = server
    session.execute.side_effect = execute
    api = NS(get_inbounds=AsyncMock(return_value=[XUIInbound(
        id=1, settings={"clients": []},
        clientStats=[{"email": "old-name", "total": 1000, "up": 200, "down": 0}],
    )]), patch_client=AsyncMock())

    @asynccontextmanager
    async def factory(server):
        yield api

    repo = MagicMock(get_service_security_settings=AsyncMock(return_value=NS(xui_limit_ip=1)))
    manager = mod.ProvisioningManager(session)
    with patch.object(mod, "is_marzban_family", return_value=False), \
         patch.object(mod, "AppSettingsRepository", return_value=repo), \
         patch.object(manager, "_get_xui_client_for_server", factory):
        result = await manager.reconcile_migrated_usage_for_server(server)
    assert result["fixed"] == 1
    assert committed["volume_bytes"] == 800  # paid quota of 2000 was lost
    assert "state_version" not in updates[0]
    assert api.patch_client.call_args.kwargs["client"].total_gb == 800
