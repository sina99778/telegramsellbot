"""Regression coverage for audit payment binding and transfer identity repairs."""
from contextlib import asynccontextmanager
from decimal import Decimal
import hashlib
import hmac
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
from fastapi import HTTPException
from pydantic import SecretStr
import pytest

from apps.api.routes.webhooks import nowpayments as route


def payment(**overrides):
    values = dict(id=uuid4(), user_id=uuid4(), provider="nowpayments",
                  provider_payment_id="123", provider_invoice_id="456", order_id="order-a",
                  payment_status="waiting", kind="wallet_topup", price_amount=Decimal("1000"),
                  actually_paid=None, callback_payload={"plan_id": "keep-plan", "purpose": "direct_purchase"})
    return NS(**(values | overrides))


@asynccontextmanager
async def signed_callback(session, rows, payload):
    # Execute the actual lookup predicates against the fixture rows. PostgreSQL
    # itself is external IO; the route's emitted SQL must include the provider.
    async def lookup(statement):
        params = statement.compile().params
        return next((row for row in rows if all(
            getattr(row, key.rsplit("_", 1)[0]) == value for key, value in params.items()
        )), None)

    session.scalar.side_effect = lookup
    secret = "regression-fixture-only"
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    request = NS(body=AsyncMock(return_value=body), headers={"x-nowpayments-sig": signature})
    repo = MagicMock(get_gateway_settings=AsyncMock(return_value=NS(nowpayments_ipn_secret=secret)))
    credit = AsyncMock(return_value=True)
    with patch("repositories.settings.AppSettingsRepository", return_value=repo), \
         patch.object(route, "process_successful_payment", credit):
        yield request, credit


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["payment", "order", "invoice"])
@pytest.mark.parametrize("include_now", [False, True])
async def test_nowpayments_lookups_exclude_other_provider(mock_session, path, include_now):
    fields = dict(provider_payment_id="123" if path == "payment" else None,
                  order_id="order-a" if path != "invoice" else None)
    manual = payment(provider="manual_crypto", **fields)
    now = payment(**fields)
    payload = dict(payment_id=123, payment_status="finished", invoice_id=456)
    if path != "invoice":
        payload["order_id"] = "order-a"
    async with signed_callback(mock_session, [manual, now] if include_now else [manual], payload) as (request, credit):
        if include_now:
            assert await route.handle_nowpayments_ipn(request, mock_session) == {"status": "processed"}
            assert credit.call_args.kwargs["payment"] is now
        else:
            with pytest.raises(HTTPException) as exc:
                await route.handle_nowpayments_ipn(request, mock_session)
            assert exc.value.status_code == 404
            credit.assert_not_awaited()
    assert manual.payment_status == "waiting"
    assert manual.callback_payload == {"plan_id": "keep-plan", "purpose": "direct_purchase"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,payload_ids", [
    ({}, {"order_id": "other-order"}),
    ({}, {"invoice_id": 999}),
    ({"provider_payment_id": "other-payment"}, {}),
    ({"provider_payment_id": None}, {"invoice_id": 999}),
    ({"provider_payment_id": None}, {"order_id": "other-order"}),
])
async def test_nowpayments_rejects_conflicting_identifiers_before_mutation(mock_session, stored, payload_ids):
    row = payment(**stored)
    original = vars(row).copy()
    payload = dict(payment_id=123, payment_status="finished", order_id="order-a", invoice_id=456) | payload_ids
    async with signed_callback(mock_session, [row], payload) as (request, credit):
        with pytest.raises(HTTPException) as exc:
            await route.handle_nowpayments_ipn(request, mock_session)
        assert exc.value.status_code == 409
        credit.assert_not_awaited()
    assert vars(row) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["order", "invoice"])
async def test_invoice_first_callback_binds_payment_and_preserves_metadata(mock_session, path):
    row = payment(provider_payment_id=None)
    payload = dict(payment_id=123, payment_status="finished", invoice_id=456)
    if path == "order":
        payload["order_id"] = "order-a"
    async with signed_callback(mock_session, [row], payload) as (request, credit):
        assert await route.handle_nowpayments_ipn(request, mock_session) == {"status": "processed"}
        assert credit.call_args.kwargs["amount_to_credit"] == Decimal("1000")
    assert row.provider_payment_id == "123"
    assert row.callback_payload == {"plan_id": "keep-plan", "purpose": "direct_purchase", "nowpayments_ipn": payload}


@pytest.mark.asyncio
async def test_transfer_rotates_identity_preserving_live_panel_limits(mock_session):
    from apps.bot.handlers.user import transfer
    from services.xui.client import SanaeiXUIClient, XUIClientConfig

    old = {"id": "old", "email": "fixture", "subId": "old-sub", "flow": "keep",
           "totalGB": 9999, "expiryTime": 2000000000000, "limitIp": 7, "enable": False,
           "comment": "panel comment", "tgId": "12345", "custom": "preserve"}
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
            assert await transfer._rotate_config_identity_for_transfer(mock_session, sub)
    assert len(writes) == 1
    assert current["id"] != "old" and current["subId"] != "old-sub"
    assert record.client_uuid == current["id"]
    for key, value in old.items():
        if key not in {"id", "subId"}:
            assert current[key] == value, key
