"""Fault-injection regressions for the October upgrade. No external services."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from models.subscription import Subscription
from schemas.internal.xui import XUIClient, XUIInbound
from services.panel_sync import request_panel_sync
from services.xui.client import SanaeiXUIClient, XUIClientConfig, XUIRequestError


def state(**overrides):
    values = dict(
        id=uuid4(),
        status="active",
        plan=NS(),
        xui_client=NS(is_active=True),
        volume_bytes=100,
        used_bytes=30,
        ends_at=None,
        sub_link="https://sub.invalid/fixture",
        panel_sync_pending=True,
        panel_sync_error=None,
        panel_sync_attempts=0,
        panel_sync_attempted_at=None,
    )
    return NS(**(values | overrides))


async def test_failed_remote_sync_remains_pending_and_replays_absolute_state(mock_session):
    from apps.worker.jobs import panel_sync

    sub = state()
    desired = (sub.volume_bytes, sub.used_bytes, sub.ends_at)
    repo = MagicMock()
    repo.get_service_security_settings = AsyncMock(return_value=NS(xui_limit_ip=0))
    send = AsyncMock(side_effect=[TimeoutError(), None])
    with (
        patch.object(panel_sync, "AppSettingsRepository", return_value=repo),
        patch.object(panel_sync, "_sync_xui_limits", send),
    ):
        assert not await panel_sync.deliver_panel_state(mock_session, sub)
        assert sub.panel_sync_pending and sub.panel_sync_error == "TimeoutError"
        assert sub.panel_sync_attempts == 1
        assert not await panel_sync.deliver_panel_state(mock_session, sub)  # bounded backoff
        send.assert_awaited_once()
        sub.panel_sync_attempted_at -= timedelta(minutes=2)
        assert await panel_sync.deliver_panel_state(mock_session, sub)
    assert not sub.panel_sync_pending and sub.panel_sync_error is None
    assert sub.panel_sync_attempts == 2 and send.await_count == 2
    assert (sub.volume_bytes, sub.used_bytes, sub.ends_at) == desired


async def test_failed_ban_is_retried_until_panel_confirms(mock_session):
    from apps.worker.jobs import panel_sync

    sub = state(status="disabled")
    manager = MagicMock()
    manager._disable_xui_client = AsyncMock(side_effect=[XUIRequestError("offline"), None])
    with patch.object(panel_sync, "ProvisioningManager", return_value=manager):
        assert not await panel_sync.deliver_panel_state(mock_session, sub)
        assert sub.xui_client.is_active is True  # observed state remains truthful
        assert sub.panel_sync_pending
        sub.panel_sync_attempted_at -= timedelta(minutes=2)
        assert await panel_sync.deliver_panel_state(mock_session, sub)
    assert sub.xui_client.is_active is False and not sub.panel_sync_pending


async def test_ban_queues_intent_without_mutating_panel_before_commit(mock_session):
    from services.provisioning.manager import ProvisioningManager

    sub = state(panel_sync_pending=False)
    result = MagicMock()
    result.scalars.return_value.all.return_value = [sub]
    mock_session.execute.return_value = result
    manager = ProvisioningManager(mock_session)
    manager._disable_xui_client = AsyncMock()
    assert await manager.disable_user_active_configs(uuid4()) == 1
    assert sub.status == "disabled" and sub.panel_sync_pending
    assert sub.xui_client.is_active is True
    manager._disable_xui_client.assert_not_awaited()


async def test_pending_outbox_blocks_realtime_state_overwrite(mock_session):
    from apps.worker.jobs.subscriptions import get_realtime_usage

    assert await get_realtime_usage(mock_session, state()) is None
    mock_session.scalar.assert_not_awaited()


async def test_remote_patch_preserves_vision_metadata_and_unknown_fields():
    existing = {
        "id": "fixture",
        "email": "fixture",
        "flow": "xtls-rprx-vision",
        "tgId": "12345",
        "reset": 30,
        "futurePanelField": "keep-me",
        "subId": "sub-fixture",
        "totalGB": 100,
    }
    config = XUIClientConfig("https://panel.invalid", "fixture", SecretStr("FAKE"))
    async with SanaeiXUIClient(config) as client:
        client.get_inbounds = AsyncMock(
            return_value=[
                XUIInbound(
                    id=1,
                    protocol="vless",
                    settings={"clients": [existing]},
                )
            ]
        )
        client.update_client = AsyncMock()
        await client.patch_client(
            inbound_id=1,
            client_id="fixture",
            client=XUIClient(id="fixture", email="fixture", totalGB=200),
        )
        sent = client.update_client.call_args.kwargs["client"].to_xui_payload()
    assert sent["flow"] == "xtls-rprx-vision" and sent["tgId"] == "12345"
    assert sent["reset"] == 30 and sent["subId"] == "sub-fixture"
    assert sent["futurePanelField"] == "keep-me" and sent["totalGB"] == 200


async def test_missing_remote_client_refuses_blind_replacement():
    config = XUIClientConfig("https://panel.invalid", "fixture", SecretStr("FAKE"))
    async with SanaeiXUIClient(config) as client:
        client.get_inbounds = AsyncMock(return_value=[])
        client.update_client = AsyncMock()
        with pytest.raises(XUIRequestError):
            await client.patch_client(
                inbound_id=1,
                client_id="fixture",
                client=XUIClient(id="fixture", email="fixture"),
            )
        client.update_client.assert_not_awaited()


@pytest.mark.parametrize("location", ["https://other.invalid/login", "http://panel.invalid/login"])
async def test_sensitive_login_redirect_rejected_before_credentials_leave_origin(location):
    seen = []

    def transport(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": location})

    config = XUIClientConfig("https://panel.invalid/", "fixture", SecretStr("FAKE"))
    async with httpx.AsyncClient(
        base_url=config.base_url,
        follow_redirects=True,
        transport=httpx.MockTransport(transport),
    ) as http:
        async with SanaeiXUIClient(config, http_client=http) as client:
            with pytest.raises(XUIRequestError):
                await client.login()
    assert seen and all(url == "https://panel.invalid/login" for url in seen)


async def test_subscription_fetch_dials_validated_ip_preserving_host_and_tls_sni(monkeypatch):
    import socket

    from services.provisioning.manager import ProvisioningManager

    seen = []
    lookups = []

    def resolve(host, port, **kwargs):
        lookups.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

    def transport(request):
        seen.append(request)
        return httpx.Response(
            200, headers={"subscription-userinfo": "total=1234; expire=2000000000"}
        )

    original = httpx.AsyncClient

    def isolated_client(*args, **kwargs):
        assert kwargs["trust_env"] is False
        return original(*args, **kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpx, "AsyncClient", isolated_client)
    result = await ProvisioningManager(MagicMock())._recover_from_sublink_header(
        NS(sub_link="https://sub.invalid/sub/FAKE", legacy_link=None),
    )
    assert result == (1234, 2000000000000)
    assert lookups == ["sub.invalid"] and len(seen) == 1
    assert seen[0].url.host == "8.8.8.8" and seen[0].headers["host"] == "sub.invalid"
    assert seen[0].extensions["sni_hostname"] == "sub.invalid"


async def test_redis_has_bounded_network_timeouts():
    import core.redis as redis_module

    with patch.object(redis_module, "_redis_client", None):
        client = redis_module.get_redis()
        values = client.connection_pool.connection_kwargs
        assert 0 < values["socket_timeout"] <= 5
        assert 0 < values["socket_connect_timeout"] <= 5
        await redis_module.close_redis()


async def test_first_use_unlimited_time_plan_stays_active(mock_session):
    from apps.worker.jobs import subscriptions as sync

    sub = state(
        status="pending_activation",
        activated_at=None,
        starts_at=None,
        ends_at=None,
        plan=NS(duration_days=0),
        panel_sync_pending=False,
        usage_sync_failures=0,
        last_usage_sync_at=None,
        expired_at=None,
    )
    sub.xui_client = NS(
        id=uuid4(),
        email="fixture",
        username="fixture",
        sub_link=sub.sub_link,
        client_uuid="fixture",
        xui_client_remote_id="fixture",
        is_active=True,
        inbound=NS(xui_inbound_remote_id=1),
    )
    client = MagicMock()
    client.get_inbounds = AsyncMock(
        return_value=[
            XUIInbound(
                id=1,
                settings={"clients": [{"id": "fixture", "email": "fixture"}]},
                clientStats=[{"email": "fixture", "up": 1, "down": 0}],
            )
        ]
    )
    client.patch_client = AsyncMock()
    with patch.object(sync, "_get_ip_abuse", AsyncMock(return_value=None)):
        assert not await sync.sync_xui_usage_and_status(
            mock_session, client, [sub], NS(xui_limit_ip=1)
        )
    assert sub.status == "active" and sub.ends_at is None
    assert client.patch_client.call_args.kwargs["client"].expiry_time == 0
    assert client.get_inbounds.await_count == 1


async def test_readiness_reports_dependency_failure_without_exposing_details(mock_session):
    from fastapi import HTTPException

    from apps.api import main

    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=mock_session)
    context.__aexit__ = AsyncMock(return_value=False)
    redis = MagicMock(ping=AsyncMock(side_effect=TimeoutError("FAKE-SECRET")))
    with (
        patch.object(main, "AsyncSessionFactory", return_value=context),
        patch("core.redis.get_redis", return_value=redis),
    ):
        with pytest.raises(HTTPException) as error:
            await main.healthz()
    assert error.value.status_code == 503 and "FAKE-SECRET" not in error.value.detail


def test_ipv6_urls_and_unsupported_protocol():
    from urllib.parse import urlsplit

    from services.xui.runtime import build_sub_link, build_vless_uri

    server = NS(
        base_url="https://[2001:db8::1]:54321/admin",
        sub_domain=None,
        subscription_port=2096,
        config_domain="2001:db8::2",
    )
    url = urlsplit(build_sub_link(server, "fixture"))
    assert url.hostname == "2001:db8::1" and url.port == 2096
    with pytest.raises(ValueError, match="Unsupported"):
        build_vless_uri(
            client_uuid="fixture",
            server=server,
            inbound=NS(protocol="trojan", port=443),
            sub_id="fixture",
        )


def test_proxy_trust_is_restricted_to_route_gateway():
    from scripts.run_api import trusted_proxy_ips

    routes = (
        "Iface Destination Gateway Flags\neth0 00000000 010012AC 0003\neth0 000012AC 00000000 0001"
    )
    assert trusted_proxy_ips(routes) == "127.0.0.1,::1,172.18.0.1"
    assert "*" not in trusted_proxy_ips(routes)


def test_subscription_version_prevents_stale_usage_writer_from_losing_outbox():
    assert Subscription.__mapper__.version_id_col.name == "state_version"
    sub = state(panel_sync_pending=False, panel_sync_error="TimeoutError", panel_sync_attempts=3)
    request_panel_sync(sub)
    assert sub.panel_sync_pending and sub.panel_sync_error is None
    assert sub.panel_sync_attempts == 0 and sub.panel_sync_attempted_at is None


async def test_gateway_preflight_rejection_prevents_debit_and_provision(mock_session):
    from services import payment as payment_module

    session = mock_session
    plan_id = uuid4()
    payment = NS(
        id=uuid4(),
        user_id=uuid4(),
        kind="direct_purchase",
        provider="nowpayments",
        price_amount=Decimal("12"),
        actually_paid=Decimal("12"),
        payment_status="finished",
        callback_payload={"plan_id": str(plan_id), "config_name": "FixtureVPN"},
    )
    user = MagicMock(id=payment.user_id, telegram_id=12345, wallet=MagicMock())
    plan = MagicMock(
        id=plan_id,
        price=Decimal("12"),
        currency="USD",
        volume_bytes=1024**3,
        duration_days=30,
        name="Fixture",
        code="fixture",
    )
    session.scalar.return_value = user
    session.get.return_value = plan
    manager = MagicMock()
    manager.provision_subscription = AsyncMock(
        return_value=NS(
            subscription=NS(id=uuid4()),
            sub_link="https://sub.invalid/fixture",
            vless_uri="vless://fixture",
        )
    )
    coroutine_objects = []
    checked = []

    async def reject():
        checked.append(True)
        return False, "panel offline"

    def preflight(*args):
        value = reject()
        coroutine_objects.append(value)
        return value

    manager.preflight_check_plan.side_effect = preflight
    wallet = MagicMock(process_transaction=AsyncMock())
    bot = MagicMock(send_message=AsyncMock(), send_photo=AsyncMock())
    bot.session.close = AsyncMock()
    try:
        with (
            patch.object(payment_module, "WalletManager", return_value=wallet),
            patch.object(payment_module, "_get_shared_bot", return_value=bot),
            patch("services.provisioning.manager.ProvisioningManager", return_value=manager),
            patch.object(payment_module, "_process_gateway_referral_bonus", AsyncMock()),
            patch("core.qr.make_qr_bytes", return_value=None),
            patch("services.sales_notifications.notify_purchase", AsyncMock()),
        ):
            result = await payment_module._handle_direct_purchase(session, payment)
        assert result is False and checked == [True]
        manager.provision_subscription.assert_not_awaited()
        wallet.process_transaction.assert_not_awaited()
    finally:
        for value in coroutine_objects:
            value.close()


async def test_paid_webhook_credits_settlement_even_when_renewal_is_busy(mock_session):
    import hashlib
    import hmac
    import json

    from starlette.requests import Request

    from apps.api.dependencies import db
    from apps.api.routes.webhooks.nowpayments import handle_nowpayments_ipn
    from apps.worker.jobs.payments import _is_sweepable

    session = mock_session
    payment = NS(
        id=uuid4(),
        user_id=uuid4(),
        provider="nowpayments",
        kind="direct_renewal",
        price_amount=Decimal("10"),
        actually_paid=None,
        price_currency="USD",
        payment_status="waiting",
        provider_payment_id="fixture-payment",
        callback_payload={"sub_id": str(uuid4()), "renew_type": "time", "renew_amount": 10},
    )
    session.scalar.return_value = payment
    payload = {"payment_id": "fixture-payment", "payment_status": "finished"}
    body = json.dumps(payload).encode()
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    secret = "FAKE-AUDIT-SECRET"
    signature = hmac.new(secret.encode(), canonical, hashlib.sha512).hexdigest()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {"type": "http", "method": "POST", "headers": [(b"x-nowpayments-sig", signature.encode())]},
        receive,
    )
    repo = MagicMock()
    repo.get_gateway_settings = AsyncMock(return_value=NS(nowpayments_ipn_secret=secret))
    wallet = MagicMock(process_transaction=AsyncMock())
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)

    @asynccontextmanager
    async def busy(*args, **kwargs):
        yield False

    with (
        patch("repositories.settings.AppSettingsRepository", return_value=repo),
        patch("core.redis.distributed_lock", busy),
        patch("services.payment.WalletManager", return_value=wallet),
        patch.object(db, "AsyncSessionFactory", return_value=context),
    ):
        dependency = db.get_db_session()
        opened = await anext(dependency)
        result = await handle_nowpayments_ipn(request, opened)
        with pytest.raises(StopAsyncIteration):
            await anext(dependency)
    assert result == {"status": "accepted_deferred"}
    assert payment.payment_status == "finished" and payment.actually_paid == Decimal("10")
    assert not _is_sweepable(payment)
    wallet.process_transaction.assert_awaited_once()
    assert wallet.process_transaction.call_args.kwargs["direction"] == "credit"
    session.commit.assert_awaited_once()
