"""Tests for the PasarGuard HTTP client (mock httpx transport — no real panel)."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import SecretStr

from schemas.internal.pasarguard import PGUserCreate
from services.pasarguard.client import (
    PasarGuardClient,
    PasarGuardClientConfig,
    PasarGuardRequestError,
)


def _make_client(handler) -> PasarGuardClient:
    http = httpx.AsyncClient(
        base_url="http://panel.local/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    cfg = PasarGuardClientConfig(
        base_url="http://panel.local", username="admin", password=SecretStr("pw")
    )
    return PasarGuardClient(cfg, http_client=http)


@pytest.mark.asyncio
async def test_login_posts_form_and_caches_bearer_token():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            seen["ct"] = request.headers.get("content-type", "")
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"access_token": "tok123", "token_type": "bearer"})
        if request.url.path == "/api/admin":
            seen["auth"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={"username": "admin"})
        return httpx.Response(404)

    client = _make_client(handler)
    await client.login()
    assert "application/x-www-form-urlencoded" in seen["ct"]
    assert "username=admin" in seen["body"]
    assert "grant_type=password" in seen["body"]

    await client.get_current_admin()
    assert seen["auth"] == "Bearer tok123"  # token cached on the auth header
    await client.aclose()


@pytest.mark.asyncio
async def test_create_user_omits_proxy_settings_and_parses_response():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        if request.url.path == "/api/user" and request.method == "POST":
            captured["body"] = json.loads(request.content.decode())
            return httpx.Response(
                201,
                json={
                    "id": 7,
                    "username": "u1",
                    "status": "on_hold",
                    "used_traffic": 0,
                    "subscription_url": "/sub/abc/",
                },
            )
        return httpx.Response(404)

    client = _make_client(handler)
    resp = await client.create_user(
        PGUserCreate(
            username="u1",
            status="on_hold",
            data_limit=1024,
            group_ids=[2],
            on_hold_expire_duration=100,
        )
    )
    # proxy_settings is never sent → panel auto-generates all protocols.
    assert "proxy_settings" not in captured["body"]
    # on_hold create must NOT send an explicit expire.
    assert "expire" not in captured["body"]
    assert captured["body"]["group_ids"] == [2]
    assert resp.username == "u1"
    assert resp.subscription_url == "/sub/abc/"
    await client.aclose()


@pytest.mark.asyncio
async def test_reauths_once_on_401():
    state = {"tokens": 0, "admin_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            state["tokens"] += 1
            return httpx.Response(
                200, json={"access_token": f"tok{state['tokens']}", "token_type": "bearer"}
            )
        if request.url.path == "/api/admin":
            state["admin_calls"] += 1
            # First admin call carries the stale tok1 → 401; after a forced
            # re-login (tok2) the retry succeeds.
            if request.headers.get("authorization") == "Bearer tok1":
                return httpx.Response(401, json={"detail": "token expired"})
            return httpx.Response(200, json={"username": "admin"})
        return httpx.Response(404)

    client = _make_client(handler)
    await client.login()  # tok1
    result = await client.get_current_admin()  # 401 → relogin tok2 → 200
    assert result == {"username": "admin"}
    assert state["tokens"] == 2  # logged in twice
    assert state["admin_calls"] == 2  # original + retry
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_initial_requests_share_one_login():
    state = {"tokens": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            state["tokens"] += 1
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        if request.url.path == "/api/admin":
            return httpx.Response(200, json={"username": "admin"})
        return httpx.Response(404)

    client = _make_client(handler)
    await asyncio.gather(*(client.get_current_admin() for _ in range(5)))
    assert state["tokens"] == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_unauthorized_requests_share_one_reauthentication():
    state = {"tokens": 0}
    stale_requests = 0
    stale_ready = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stale_requests
        if request.url.path == "/api/admin/token":
            state["tokens"] += 1
            return httpx.Response(
                200, json={"access_token": f"tok{state['tokens']}", "token_type": "bearer"}
            )
        if request.url.path == "/api/admin":
            if request.headers.get("authorization") == "Bearer tok1":
                stale_requests += 1
                if stale_requests == 5:
                    stale_ready.set()
                await stale_ready.wait()
                return httpx.Response(401, json={"detail": "token expired"})
            return httpx.Response(200, json={"username": "admin"})
        return httpx.Response(404)

    client = _make_client(handler)
    await client.login()
    await asyncio.gather(*(client.get_current_admin() for _ in range(5)))
    assert state["tokens"] == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_transient_5xx_retries_only_idempotent_methods(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    state = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state[request.method.lower()] += 1
        return httpx.Response(503)

    client = _make_client(handler)
    get_response = await client._send("GET", "api/groups")
    post_response = await client._send("POST", "api/user")
    assert get_response.status_code == 503
    assert post_response.status_code == 503
    assert state == {"get": 3, "post": 1}
    await client.aclose()


@pytest.mark.asyncio
async def test_transport_errors_retry_post(monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] < 3:
            raise httpx.ConnectError("disconnected", request=request)
        return httpx.Response(201)

    client = _make_client(handler)
    response = await client._send("POST", "api/user")
    assert response.status_code == 201
    assert state["calls"] == 3
    await client.aclose()


@pytest.mark.asyncio
async def test_get_user_404_returns_none_and_delete_404_is_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        if request.url.path == "/api/user/ghost":
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(404)

    client = _make_client(handler)
    assert await client.get_user("ghost") is None
    await client.delete_user("ghost")  # must not raise (already gone)
    await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_status_raises_with_status_code():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        if request.url.path == "/api/user":
            return httpx.Response(409, json={"detail": "username already exists"})
        return httpx.Response(404)

    client = _make_client(handler)
    with pytest.raises(PasarGuardRequestError) as ei:
        await client.create_user(PGUserCreate(username="u1", group_ids=[1]))
    assert ei.value.status_code == 409
    await client.aclose()
