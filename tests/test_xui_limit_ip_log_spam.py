"""
Regression tests for the 3x-ui panel log spam:

    WARNING - X-UI: client ip job err:invalid argument     (every 10 seconds)

Root cause chain (verified against MHSanaei/3x-ui sources):

  1. 3x-ui schedules CheckClientIpJob `@every 10s`.
  2. Its Run() only reaches the broken code path when `hasLimitIp()` is true,
     i.e. when SOME client carries `limitIp > 0`. updateInboundClientIps sets
     `shouldCleanLog = true` unconditionally for such a client (not only when
     the limit is exceeded), so clearAccessLog() then runs on EVERY tick.
  3. clearAccessLog() calls `j.checkError(err)` after `os.Open(accessLogPath)`
     — checkError only LOGS, it does not return — so a failed open leaves a
     nil *os.File which `io.Copy` rejects with os.ErrInvalid, whose message is
     literally "invalid argument".

Our bot armed that path: it stamped `limitIp=1` on every client it created or
touched, because the default `xui_limit_ip` was 1. The cap was inert anyway
(enforcement needs the Xray access log configured AND fail2ban installed in
the panel container), so the default is now 0 and the sync job heals leftover
clients that still hold a non-zero value.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from apps.worker.jobs.subscriptions import sync_xui_usage_and_status
from repositories.settings import DEFAULT_XUI_LIMIT_IP, AppSettingsRepository
from services.xui.client import XUIRequestError

pytestmark = pytest.mark.asyncio


def _sub(email="cfg-1", client_id="client-1"):
    sub = MagicMock()
    sub.id = 1
    sub.status = "active"
    sub.volume_bytes = 100
    sub.used_bytes = 0
    sub.usage_sync_failures = 0
    sub.activated_at = MagicMock()
    sub.ends_at = None
    sub.expired_at = None
    sub.sub_link = ""
    sub.plan = MagicMock(duration_days=30)
    record = MagicMock()
    record.email = email
    record.is_active = True
    record.inbound.xui_inbound_remote_id = 7
    record.xui_client_remote_id = client_id
    record.client_uuid = "uuid-1"
    record.sub_link = ""
    sub.xui_client = record
    return sub


def _inbound(*clients, inbound_id=7):
    """Fake /inbounds/list entry carrying an explicit limitIp per client."""
    return MagicMock(
        id=inbound_id,
        settings={"clients": list(clients)},
        client_stats=[
            {"email": c["email"], "up": 1, "down": 1} for c in clients
        ],
    )


def _settings(limit_ip=DEFAULT_XUI_LIMIT_IP):
    return MagicMock(
        xui_limit_ip=limit_ip,
        max_distinct_ips=0,
        auto_disable_ip_abuse=False,
        restart_xray_on_expiry=False,
    )


# ── The default itself ────────────────────────────────────────────────────

async def test_default_xui_limit_ip_is_zero():
    # 0 == unlimited on X-UI and is what keeps hasLimitIp() false, so the
    # panel's 10-second broken clearAccessLog() path is never armed.
    assert DEFAULT_XUI_LIMIT_IP == 0


async def test_security_settings_default_does_not_arm_the_panel_job():
    session = AsyncMock()
    session.get = AsyncMock(return_value=None)  # no stored AppSetting row

    settings = await AppSettingsRepository(session).get_service_security_settings()

    assert settings.xui_limit_ip == 0


async def test_stored_settings_without_the_key_fall_back_to_zero():
    # A row written before this key existed must not resurrect limitIp=1.
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(value_json={"max_distinct_ips": 5}))

    settings = await AppSettingsRepository(session).get_service_security_settings()

    assert settings.xui_limit_ip == 0
    assert settings.max_distinct_ips == 5


async def test_admin_can_still_opt_into_a_nonzero_limit():
    # The setting stays configurable — we only changed the DEFAULT.
    session = AsyncMock()
    session.get = AsyncMock(return_value=MagicMock(value_json={"xui_limit_ip": 3}))

    settings = await AppSettingsRepository(session).get_service_security_settings()

    assert settings.xui_limit_ip == 3


# ── The self-healing backfill ─────────────────────────────────────────────

async def test_sync_heals_leftover_limit_ip_on_the_panel():
    # THE fix for an existing deployment: a single client left at limitIp=1
    # keeps 3x-ui's CheckClientIpJob firing every 10s, so the sync must push
    # limitIp=0 for clients provisioned before the default changed.
    sub = _sub()
    inbound = _inbound({
        "id": "client-1", "uuid": "client-1", "email": "cfg-1",
        "limitIp": 1, "totalGB": 500, "expiryTime": 123, "enable": True,
        "subId": "sub-abc", "comment": "user:1",
    })
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings())

    xui_client.update_client.assert_awaited_once()
    sent = xui_client.update_client.await_args.kwargs["client"]
    assert sent.limit_ip == 0
    # Every other field echoes the PANEL's values — healing limitIp must not
    # clobber quota/expiry/subId that the panel considers authoritative.
    assert sent.total_gb == 500
    assert sent.expiry_time == 123
    assert sent.sub_id == "sub-abc"
    assert sent.comment == "user:1"
    assert sent.enable is True


async def test_sync_does_not_touch_clients_already_at_the_desired_limit():
    # Idempotence: once healed, the 1-minute sweep must stop writing to the
    # panel — otherwise we'd add a per-config write on every single cycle.
    sub = _sub()
    inbound = _inbound({
        "id": "client-1", "uuid": "client-1", "email": "cfg-1",
        "limitIp": 0, "totalGB": 500, "expiryTime": 123, "enable": True,
    })
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings())

    xui_client.update_client.assert_not_awaited()


async def test_missing_limit_ip_field_is_treated_as_zero():
    # Panels that omit limitIp entirely already behave as unlimited; writing
    # to them would be a pointless per-cycle panel call.
    sub = _sub()
    inbound = _inbound({
        "id": "client-1", "uuid": "client-1", "email": "cfg-1", "totalGB": 10,
    })
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings())

    xui_client.update_client.assert_not_awaited()


async def test_backfill_respects_an_admin_configured_nonzero_limit():
    # An admin who deliberately sets 3 must have 3 pushed, not 0.
    sub = _sub()
    inbound = _inbound({
        "id": "client-1", "uuid": "client-1", "email": "cfg-1",
        "limitIp": 1, "totalGB": 10, "expiryTime": 0, "enable": True,
    })
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings(limit_ip=3))

    sent = xui_client.update_client.await_args.kwargs["client"]
    assert sent.limit_ip == 3


async def test_failed_backfill_never_breaks_the_usage_sync():
    # Best-effort: the panel refusing the normalization must not cost us the
    # usage figures for this cycle (nor count as a "client gone" strike).
    sub = _sub()
    inbound = _inbound({
        "id": "client-1", "uuid": "client-1", "email": "cfg-1",
        "limitIp": 1, "totalGB": 10, "expiryTime": 0, "enable": True,
    })
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[inbound])
    xui_client.update_client = AsyncMock(side_effect=XUIRequestError("nope"))

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings())

    assert sub.used_bytes == 2          # up+down still recorded
    assert sub.usage_sync_failures == 0  # not a "client gone" strike


async def test_absent_client_is_not_backfilled():
    # A client missing from the panel must go down the strike path, not have
    # an update_client call attempted against a client that isn't there.
    sub = _sub()
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[_inbound()])

    await sync_xui_usage_and_status(AsyncMock(), xui_client, [sub], _settings())

    xui_client.update_client.assert_not_awaited()
    assert sub.usage_sync_failures == 1
