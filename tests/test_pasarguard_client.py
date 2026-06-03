"""Tests for the PasarGuard HTTP client (mock httpx transport — no real panel)."""
from __future__ import annotations

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
