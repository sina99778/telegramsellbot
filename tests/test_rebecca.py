"""Tests for the Rebecca panel client + Marzban-family dispatch (rebecca)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import httpx
import pytest
from pydantic import SecretStr

from services.rebecca.client import RebeccaClient, RebeccaClientConfig


def _make_client(handler) -> RebeccaClient:
    http = httpx.AsyncClient(
        base_url="http://panel.local/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    cfg = RebeccaClientConfig(base_url="http://panel.local", username="admin", password=SecretStr("pw"))
    return RebeccaClient(cfg, http_client=http)


@pytest.mark.asyncio
async def test_create_user_in_bundle_uses_service_id_not_group_ids():
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t", "token_type": "bearer"})
        if req.url.path == "/api/user" and req.method == "POST":
            captured["body"] = json.loads(req.content.decode())
            return httpx.Response(201, json={"id": 7, "username": "u1", "status": "on_hold", "used_traffic": 0, "subscription_url": "/sub/abc/"})
        return httpx.Response(404)

    client = _make_client(handler)
    resp = await client.create_user_in_bundle(
        username="u1", status="on_hold", expire=None, data_limit=1024, bundle_id=3, on_hold_expire_duration=100
    )
    body = captured["body"]
    assert body["service_id"] == 3        # Rebecca bundle is a service_id
    assert "group_ids" not in body        # NOT PasarGuard's group_ids
    assert "expire" not in body           # on_hold omits expire
    assert body["on_hold_expire_duration"] == 100
    assert resp.username == "u1"
    assert resp.subscription_url == "/sub/abc/"
    await client.aclose()


@pytest.mark.asyncio
async def test_list_bundles_maps_services_to_remote_groups():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t"})
        if req.url.path == "/api/v2/services":
            return httpx.Response(200, json={
                "services": [
                    {"id": 1, "name": "germany", "host_count": 3},
                    {"id": 2, "name": "france", "host_count": 0},
                ],
                "total": 2,
            })
        return httpx.Response(404)

    client = _make_client(handler)
    bundles = await client.list_bundles()
    assert [(b.remote_id, b.name) for b in bundles] == [(1, "germany"), (2, "france")]
    assert all(not b.is_disabled for b in bundles)
    await client.aclose()


@pytest.mark.asyncio
async def test_get_user_404_returns_none():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(404)

    client = _make_client(handler)
    assert await client.get_user("ghost") is None
    await client.aclose()


def test_rebecca_dispatch_is_marzban_family():
    from services.panels.marzban import is_marzban_family, record_is_marzban_family
    from services.panels.registry import is_known_panel_type, strategy_for_record, strategy_for_server

    assert strategy_for_server(NS(panel_type="rebecca")).kind == "rebecca"
    assert strategy_for_record(NS(panel_kind="rebecca")).kind == "rebecca"
    assert is_known_panel_type("rebecca")
    assert is_marzban_family(NS(panel_type="rebecca"))
    assert record_is_marzban_family(NS(panel_kind="rebecca"))


@pytest.mark.asyncio
async def test_marzban_client_for_server_dispatches_by_panel_type(monkeypatch):
    import services.panels.marzban as m
    import services.pasarguard.runtime as pr
    import services.rebecca.runtime as rr

    @asynccontextmanager
    async def fake_rebecca(server):
        yield "rebecca-client"

    @asynccontextmanager
    async def fake_pg(server):
        yield "pg-client"

    monkeypatch.setattr(rr, "create_rebecca_client_for_server", fake_rebecca)
    monkeypatch.setattr(pr, "create_pasarguard_client_for_server", fake_pg)

    async with m.marzban_client_for_server(NS(panel_type="rebecca")) as c:
        assert c == "rebecca-client"
    async with m.marzban_client_for_server(NS(panel_type="pasarguard")) as c:
        assert c == "pg-client"
