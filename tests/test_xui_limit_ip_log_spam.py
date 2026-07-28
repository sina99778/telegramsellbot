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

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.worker.jobs.subscriptions import (
    normalize_server_limit_ip,
    sync_xui_usage_and_status,
)
from repositories.settings import DEFAULT_XUI_LIMIT_IP, AppSettingsRepository
from services.xui.client import XUIRequestError

pytestmark = pytest.mark.asyncio


def _factory(session):
    """Stand in for AsyncSessionFactory() used as an async context manager."""

    @asynccontextmanager
    async def _cm():
        yield session

    return lambda: _cm()


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
            {"email": c["email"], "up": 1, "down": 1} for c in clients if c.get("email")
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


# ── Gap 1: a saved settings row overrode the new default ──────────────────
# The first fix only changed the FALLBACK. Any admin who had opened the
# security settings and saved had `xui_limit_ip: 1` persisted, and the stored
# value wins over the default forever — so the bot kept re-asserting limitIp=1
# and the panel kept spamming after the upgrade. Migration 006 clears it.

def _migration_006():
    import importlib.util

    path = (
        Path(__file__).resolve().parents[1]
        / "scripts" / "migrations" / "006_reset_xui_limit_ip.py"
    )
    spec = importlib.util.spec_from_file_location("migration_006", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_migration_clears_a_stored_legacy_limit_ip():
    module = _migration_006()
    record = MagicMock(value_json={"xui_limit_ip": 1, "max_distinct_ips": 3})
    session = AsyncMock()
    session.add = MagicMock()  # Session.add is sync
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=record))
    )

    with patch.object(module, "AsyncSessionFactory", _factory(session)):
        assert await module._run() == 0

    assert record.value_json["xui_limit_ip"] == 0
    assert record.value_json["max_distinct_ips"] == 3  # untouched
    session.commit.assert_awaited()


async def test_migration_keeps_a_deliberate_admin_value():
    # Only the historical default (1) is cleared. An admin who chose 3 meant it.
    module = _migration_006()
    record = MagicMock(value_json={"xui_limit_ip": 3})
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=record))
    )

    with patch.object(module, "AsyncSessionFactory", _factory(session)):
        assert await module._run() == 0

    assert record.value_json["xui_limit_ip"] == 3
    session.commit.assert_not_awaited()


async def test_migration_is_idempotent_and_safe_without_a_row():
    module = _migration_006()
    for stored in ({"xui_limit_ip": 0}, {}, None):
        record = None if stored is None else MagicMock(value_json=stored)
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=record))
        )
        with patch.object(module, "AsyncSessionFactory", _factory(session)):
            assert await module._run() == 0
        session.commit.assert_not_awaited()


# ── Gap 2: the per-sub backfill never reached most panel clients ──────────
# 3x-ui's hasLimitIp() scans EVERY client of EVERY inbound, so one leftover
# expired/disabled/hand-made client keeps the broken 10s job armed. The
# per-subscription healing only sees active/pending subs, so a panel-wide
# sweep is what actually stops the spam.

async def test_sweep_covers_clients_with_no_subscription_at_all():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound(
            {"id": "a", "email": "expired-old", "limitIp": 1, "totalGB": 5},
            {"id": "b", "email": "made-by-hand", "limitIp": 2, "totalGB": 0},
            {"id": "c", "email": "already-fine", "limitIp": 0, "totalGB": 1},
        ),
    ])

    fixed = await normalize_server_limit_ip(xui_client, desired_limit_ip=0)

    assert fixed == 2  # only the two leftovers, not the converged one
    emails = {c.kwargs["client"].email for c in xui_client.update_client.await_args_list}
    assert emails == {"expired-old", "made-by-hand"}
    assert all(c.kwargs["client"].limit_ip == 0 for c in xui_client.update_client.await_args_list)


async def test_sweep_walks_every_inbound():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound({"id": "a", "email": "in-one", "limitIp": 1}, inbound_id=1),
        _inbound({"id": "b", "email": "in-two", "limitIp": 1}, inbound_id=2),
    ])

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=0) == 2
    assert {c.kwargs["inbound_id"] for c in xui_client.update_client.await_args_list} == {1, 2}


async def test_sweep_preserves_panel_owned_fields():
    # Healing limitIp must never clobber quota/expiry/subId.
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound({
            "id": "a", "email": "keep-me", "limitIp": 1, "totalGB": 999,
            "expiryTime": 4242, "enable": False, "subId": "s-1", "comment": "c-1",
        }),
    ])

    await normalize_server_limit_ip(xui_client, desired_limit_ip=0)

    sent = xui_client.update_client.await_args.kwargs["client"]
    assert (sent.total_gb, sent.expiry_time, sent.sub_id, sent.comment) == (999, 4242, "s-1", "c-1")
    assert sent.enable is False


async def test_sweep_is_a_noop_once_the_panel_has_converged():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound({"id": "a", "email": "fine", "limitIp": 0}),
    ])

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=0) == 0
    xui_client.update_client.assert_not_awaited()


async def test_sweep_continues_past_a_failing_client():
    # One stubborn client must not abort the rest of the sweep — otherwise a
    # single bad row leaves the job armed and the spam alive.
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound(
            {"id": "a", "email": "bad", "limitIp": 1},
            {"id": "b", "email": "good", "limitIp": 1},
        ),
    ])
    xui_client.update_client = AsyncMock(side_effect=[XUIRequestError("nope"), None])

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=0) == 1
    assert xui_client.update_client.await_count == 2


async def test_sweep_survives_an_unreachable_panel():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(side_effect=XUIRequestError("down"))

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=0) == 0


async def test_sweep_honours_a_nonzero_admin_choice():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound(
            {"id": "a", "email": "x", "limitIp": 1},
            {"id": "b", "email": "y", "limitIp": 3},
        ),
    ])

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=3) == 1
    assert xui_client.update_client.await_args.kwargs["client"].email == "x"


async def test_sweep_skips_malformed_client_rows():
    xui_client = AsyncMock()
    xui_client.get_inbounds = AsyncMock(return_value=[
        _inbound(
            {"email": "no-id", "limitIp": 1},
            {"id": "b", "limitIp": 1},              # no email
            {"id": "c", "email": "ok", "limitIp": 1},
        ),
    ])

    assert await normalize_server_limit_ip(xui_client, desired_limit_ip=0) == 1
    assert xui_client.update_client.await_args.kwargs["client"].email == "ok"

