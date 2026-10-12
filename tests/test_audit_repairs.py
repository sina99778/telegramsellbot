"""Acceptance tests for the October audit; no production I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from services.crypto_claims import claim_crypto_transaction, transaction_key
from services.panels.pool import borrow, evict
from services.pricing import discounted_price
from services.xui.client import SanaeiXUIClient, XUIClientConfig, XUIAuthenticationError, XUIRequestError


def client(handler):
    http = httpx.AsyncClient(base_url="https://panel.invalid/base/", transport=httpx.MockTransport(handler))
    cfg = XUIClientConfig("https://panel.invalid/base/", "test", SecretStr("test"))
    return SanaeiXUIClient(cfg, http_client=http), http


@pytest.mark.parametrize("expired", ["html", "json", "401", "404"])
async def test_expired_session_recovers_on_same_https_address(expired):
    calls = []
    authenticated = False
    def handler(req):
        nonlocal authenticated
        calls.append(req.url.path)
        assert req.url.scheme == "https"
        if req.url.path.endswith("/login"):
            authenticated = True
            return httpx.Response(200, json={"success": True}, headers={"set-cookie": "sid=fresh; Path=/"})
        if not authenticated:
            if expired == "html":
                return httpx.Response(200, text="<html>Login</html>", headers={"content-type": "text/html"})
            if expired == "json":
                return httpx.Response(200, json={"success": False, "msg": "session expired"})
            return httpx.Response(int(expired), json={"success": False})
        return httpx.Response(200, json={"success": True, "obj": []})
    c, http = client(handler)
    c._authenticated = True
    async with http:
        assert await c.get_inbounds() == []
        assert await c.get_inbounds() == []
    assert calls.count("/base/login") == 1


async def test_parallel_reads_login_once():
    logins = 0
    async def handler(req):
        nonlocal logins
        if req.url.path.endswith("/login"):
            logins += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"success": True, "obj": []})
    c, http = client(handler)
    async with http:
        assert await asyncio.gather(*(c.get_inbounds() for _ in range(20))) == [[]]*20
    assert logins == 1


async def test_cookie_cannot_override_failed_login():
    c, http = client(lambda req: httpx.Response(200, json={"success": False}, headers={"set-cookie":"sid=bad; Path=/"}))
    http.cookies.set("sid", "stale")
    async with http:
        with pytest.raises(XUIAuthenticationError):
            await c.login()
    assert c._authenticated is False


async def test_ambiguous_auth_404_is_not_reported_as_deleted():
    c, http = client(lambda req: httpx.Response(200, json={"success": True}) if req.url.path.endswith("/login") else httpx.Response(404, json={"success": False}))
    c._authenticated = True
    async with http:
        with pytest.raises(XUIAuthenticationError):
            await c._request("POST", "panel/api/inbounds/1/delClient/uuid")


async def test_write_500_is_never_replayed():
    count = 0
    def handler(req):
        nonlocal count
        count += 1
        return httpx.Response(500, json={"success": False})
    c, http = client(handler)
    c._authenticated = True
    async with http:
        with pytest.raises(XUIRequestError):
            await c._request("POST", "panel/api/inbounds/addClient")
    assert count == 1


async def test_csrf_after_relogin_uses_new_session_token():
    authenticated = False
    writes = 0
    def handler(req):
        nonlocal authenticated, writes
        if req.url.path.endswith("/login"):
            authenticated = True
            return httpx.Response(200, json={"success": True})
        if req.url.path.endswith("/csrf-token"):
            return httpx.Response(200, json={"token": "new-session-token"})
        writes += 1
        if not authenticated:
            return httpx.Response(401, json={"success": False})
        if req.headers.get("X-CSRF-Token") != "new-session-token":
            return httpx.Response(403, text="CSRF token invalid")
        return httpx.Response(200, json={"success": True})
    c, http = client(handler)
    c._authenticated = True
    http.headers["X-CSRF-Token"] = "old-session-token"
    async with http:
        assert await c._request("POST", "panel/api/inbounds/addClient") == {"success": True}
    assert writes == 3


async def test_html_backup_is_rejected():
    def handler(req):
        if req.url.path.endswith("/login"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, text="<html>"+"login"*100, headers={"content-type":"text/html"})
    c, http = client(handler)
    async with http:
        with pytest.raises(XUIRequestError):
            await c.get_db_backup()


async def test_eviction_defers_close_and_allows_new_borrower():
    pool = {}
    factory = lambda cfg: SimpleNamespace(aclose=AsyncMock())
    async with borrow(pool, "server", "https://first", factory) as old:
        async with borrow(pool, "server", "https://first", factory) as same:
            assert same is old
            await evict(pool, "server")
            old.aclose.assert_not_awaited()
            async with borrow(pool, "server", "https://first", factory) as fresh:
                assert fresh is not old
        old.aclose.assert_not_awaited()
    old.aclose.assert_awaited_once()
    await evict(pool, "server")
    fresh.aclose.assert_awaited_once()


async def test_failed_pooled_operation_reconnects_without_url_edit():
    pool = {}
    factory = lambda cfg: SimpleNamespace(aclose=AsyncMock())
    with pytest.raises(RuntimeError):
        async with borrow(pool, "server", "https://same", factory) as failed:
            raise RuntimeError("broken connection")
    async with borrow(pool, "server", "https://same", factory) as replacement:
        assert replacement is not failed
    failed.aclose.assert_awaited_once()
    await evict(pool, "server")


@pytest.mark.parametrize("currency", ["TRX", "tron", "USDT TRC20", "USDT-TRC20", "USDT (TRC20)"])
def test_tron_transaction_identity_is_chain_global(currency):
    assert transaction_key(currency, " ABCD ") == ("TRON", "abcd")


async def test_crypto_duplicate_rejected_but_same_payment_retry_allowed():
    pid = uuid4()
    payment = SimpleNamespace(id=pid, pay_currency="TRON", provider_payment_id="ABC", callback_payload={})
    session = AsyncMock()
    session.scalar.side_effect = [None, uuid4()]
    with pytest.raises(ValueError, match="another payment"):
        await claim_crypto_transaction(session, payment)
    session.scalar.side_effect = [None, pid]
    await claim_crypto_transaction(session, payment)
    session.scalar.side_effect = [pid]
    await claim_crypto_transaction(session, payment)


@pytest.mark.parametrize("percent,expected", [(0,"10.00"),(25,"7.50"),(100,"0.00"),(-2,"10.00"),(101,"0.00")])
def test_discount_boundaries(percent, expected):
    assert discounted_price(Decimal("10"), percent) == Decimal(expected)


@pytest.mark.parametrize("percent,expected", [(25,Decimal("7.50")),(100,Decimal("0.00"))])
async def test_wallet_uses_discount_quote_and_free_purchase_skips_ledger(percent, expected):
    from apps.api.routes.miniapp.users import _purchase_with_wallet
    user = SimpleNamespace(id=uuid4(), personal_discount_percent=percent)
    plan = SimpleNamespace(id=uuid4(), price=Decimal("10"), currency="USD", code="basic")
    session = AsyncMock()
    session.add = MagicMock()
    manager = MagicMock()
    manager.provision_subscription = AsyncMock(return_value=SimpleNamespace(subscription=SimpleNamespace(id=uuid4()), sub_link="https://sub.invalid", vless_uri=None))
    wallet = MagicMock(process_transaction=AsyncMock())
    with patch("apps.api.routes.miniapp.users.ProvisioningManager", return_value=manager), patch("apps.api.routes.miniapp.users.WalletManager", return_value=wallet), patch("apps.api.routes.miniapp.users.PremiumEmojiBot", side_effect=RuntimeError("offline notifications")):
        response = await _purchase_with_wallet(session, user, plan, "vpn")
    assert response.status == "provisioned"
    assert session.add.call_args.args[0].amount == expected
    if expected == 0:
        wallet.process_transaction.assert_not_awaited()
    else:
        assert wallet.process_transaction.await_args.kwargs["amount"] == expected


async def test_miniapp_query_auth_cannot_authorize_request():
    from apps.api.routes.miniapp.users import _get_current_user
    with pytest.raises(HTTPException) as exc:
        await _get_current_user(init_data=None, _auth="leaked", _session="leaked", session_token=None, session=AsyncMock())
    assert exc.value.status_code == 401


@pytest.mark.parametrize("status,deadline,owner", [("refunded",None,"active"),("expired",None,"active"),("active",datetime.now(timezone.utc)-timedelta(seconds=1),"active"),("active",None,"banned")])
async def test_signed_subscription_does_not_bypass_revocation(status, deadline, owner):
    from apps.api.routes.sub import get_subscription_content
    sub = SimpleNamespace(status=status, ends_at=deadline, user_id=uuid4())
    session = AsyncMock()
    session.get.side_effect = [sub, SimpleNamespace(status=owner)]
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=False)
    with patch("apps.api.routes.sub.AsyncSessionFactory", return_value=cm), patch("apps.api.routes.sub.verify_subscription_signature", return_value=True), pytest.raises(HTTPException) as exc:
        await get_subscription_content(uuid4(), sig="valid")
    assert exc.value.status_code == 404
    session.scalar.assert_not_awaited()


async def test_server_capacity_lock_fails_fast_when_busy():
    from services.provisioning.manager import ProvisioningManager, ProvisioningError
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one.return_value = False
    session.execute.return_value = result
    with pytest.raises(ProvisioningError):
        await ProvisioningManager(session)._lock_server_capacity(uuid4(), ProvisioningError)
    assert "pg_try_advisory_xact_lock" in str(session.execute.await_args.args[0])


@pytest.mark.parametrize("volume", [10, 100, 20.5, 0.25])
async def test_custom_plan_volume_label_keeps_integer_zeroes(volume):
    from services.custom_purchase import create_custom_purchase_plan
    session = AsyncMock()
    session.add = MagicMock()
    template = SimpleNamespace(inbound_id=uuid4(), protocol="vless", currency="USD")
    plan = await create_custom_purchase_plan(session, volume_gb=volume, duration_days=30, price=Decimal("5"), template_plan=template)
    assert f"{volume}GB" in plan.name
    assert plan.volume_bytes == int(volume*1024**3)


@pytest.mark.parametrize("auth_date", ["9999999999999999999999999", "-9999999999999999999999", "invalid", str(int(datetime.now(timezone.utc).timestamp())+600)])
def test_invalid_or_future_telegram_timestamp_is_401(auth_date):
    from apps.api.routes.miniapp.users import validate_telegram_init_data
    from urllib.parse import urlencode
    with pytest.raises(HTTPException) as exc:
        validate_telegram_init_data(urlencode({"hash":"fake", "auth_date":auth_date, "user":"{}"}))
    assert exc.value.status_code == 401


def test_tron_and_ton_hash_representations_cannot_bypass_claims():
    import base64
    raw = bytes(range(32))
    assert transaction_key("TON", base64.b64encode(raw).decode()) == transaction_key("TON", raw.hex().upper())
    assert transaction_key("ETH", "0xABCD") == transaction_key("USDT ERC20", "0xabcd")


async def test_true_write_404_is_not_replayed_when_probe_is_authenticated():
    posts = 0
    def handler(req):
        nonlocal posts
        if req.method == "POST":
            posts += 1
            return httpx.Response(404, json={"success": False})
        return httpx.Response(200, json={"success": True, "obj": []})
    c, http = client(handler)
    c._authenticated = True
    async with http:
        with pytest.raises(XUIRequestError) as exc:
            await c._request("POST", "panel/api/inbounds/1/delClient/gone")
    assert exc.value.status_code == 404
    assert posts == 1


async def test_non_login_html_never_replays_a_write():
    posts = 0
    def handler(req):
        nonlocal posts
        posts += 1
        return httpx.Response(200, text="<html>Operation completed</html>", headers={"content-type":"text/html"})
    c, http = client(handler)
    c._authenticated = True
    async with http:
        with pytest.raises(XUIRequestError):
            await c._request("POST", "panel/api/inbounds/addClient")
    assert posts == 1


async def test_rebecca_parallel_reads_have_one_login_and_write_timeout_is_not_replayed():
    from services.rebecca.client import RebeccaClient, RebeccaClientConfig, RebeccaRequestError
    logins = writes = 0
    async def handler(req):
        nonlocal logins, writes
        if req.url.path.endswith("/token"):
            logins += 1
            await asyncio.sleep(0)
            return httpx.Response(200, json={"access_token":"test", "token_type":"bearer"})
        if req.method == "POST":
            writes += 1
            raise httpx.ReadTimeout("response lost", request=req)
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(base_url="https://panel.invalid/", transport=httpx.MockTransport(handler)) as http:
        c = RebeccaClient(RebeccaClientConfig("https://panel.invalid", "test", SecretStr("test")), http_client=http)
        await asyncio.gather(*(c.get_current_admin() for _ in range(10)))
        with pytest.raises(RebeccaRequestError):
            await c._request("POST", "api/user")
    assert logins == writes == 1


@pytest.mark.parametrize("status", [302,307])
async def test_https_redirect_cannot_send_credentials_over_http(status):
    visited = []
    def handler(req):
        visited.append(str(req.url))
        return httpx.Response(status, headers={"location":"http://panel.invalid/login"})
    c, http = client(handler)
    http.follow_redirects = True
    async with http:
        with pytest.raises(XUIRequestError, match="insecure"):
            await c._request("GET", "panel/api/inbounds/list")
    assert visited and all(url.startswith("https://") for url in visited)
