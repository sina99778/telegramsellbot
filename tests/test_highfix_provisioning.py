"""Regression tests for high-severity provisioning fixes.

Covers:
- #23: _delete_stale_panel_clients read `ib.xui_inbound_remote_id` (a DB column
  name) on XUIInbound panel schemas, raising AttributeError exactly when a
  stale client matched — cleanup silently never deleted anything.
- #24: the Marzban-family preflight calls client.get_current_admin(), which
  RebeccaClient did not implement — every Rebecca purchase/renewal preflight
  failed with "panel unreachable" while the panel was healthy.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr

from schemas.internal.xui import XUIInbound
from services.provisioning.manager import ProvisioningManager
from services.rebecca.client import RebeccaClient, RebeccaClientConfig


# ─── #23: stale-client cleanup uses the panel schema's `id` field ────────────


def _manager_with_fake_panel(mock_session, inbounds) -> tuple[ProvisioningManager, AsyncMock]:
    """ProvisioningManager whose X-UI client yields `inbounds` from get_inbounds."""
    manager = ProvisioningManager(mock_session)
    fake_api = MagicMock()
    fake_api.get_inbounds = AsyncMock(return_value=inbounds)
    fake_api.delete_client = AsyncMock()

    @asynccontextmanager
    async def _fake_cm(server):
        yield fake_api

    manager._get_xui_client_for_server = _fake_cm

    # Collateral-deletion guard query: no managed records on our side.
    exec_result = MagicMock()
    exec_result.all.return_value = []
    mock_session.execute = AsyncMock(return_value=exec_result)
    return manager, fake_api


def test_panel_inbound_schema_has_no_db_column_attribute():
    """Documents WHY the fix was needed: XUIInbound is the panel schema and
    only exposes `id` — the old code read the DB column name and crashed."""
    ib = XUIInbound.model_validate({"id": 42})
    assert ib.id == 42
    with pytest.raises(AttributeError):
        _ = ib.xui_inbound_remote_id


@pytest.mark.asyncio
async def test_stale_client_cleanup_deletes_match_via_panel_inbound_id(mock_session):
    inbound = XUIInbound.model_validate(
        {
            "id": 42,
            "settings": {
                "clients": [
                    {"id": "stale-uuid", "email": "legacy@old"},
                    {"id": "new-uuid", "email": "legacy@old"},  # freshly created
                ]
            },
        }
    )
    manager, fake_api = _manager_with_fake_panel(mock_session, [inbound])

    deleted = await manager._delete_stale_panel_clients(
        server=MagicMock(),
        remark="legacy@old",
        old_uuid=None,
        keep_uuid="new-uuid",
    )

    assert deleted == 1
    fake_api.delete_client.assert_awaited_once_with(inbound_id=42, client_id="stale-uuid")


@pytest.mark.asyncio
async def test_stale_client_cleanup_tolerates_inbound_without_id(mock_session):
    """An inbound object missing `id` is skipped defensively, not crashed on."""
    weird_inbound = NS(settings={"clients": [{"id": "stale-uuid", "email": "legacy@old"}]})
    manager, fake_api = _manager_with_fake_panel(mock_session, [weird_inbound])

    deleted = await manager._delete_stale_panel_clients(
        server=MagicMock(),
        remark="legacy@old",
        old_uuid=None,
        keep_uuid="new-uuid",
    )

    assert deleted == 0
    fake_api.delete_client.assert_not_awaited()


# ─── #24: RebeccaClient implements the get_current_admin() preflight probe ───


def _make_rebecca_client(handler) -> RebeccaClient:
    http = httpx.AsyncClient(
        base_url="http://panel.local/",
        transport=httpx.MockTransport(handler),
        headers={"Accept": "application/json"},
    )
    cfg = RebeccaClientConfig(base_url="http://panel.local", username="admin", password=SecretStr("pw"))
    return RebeccaClient(cfg, http_client=http)


@pytest.mark.asyncio
async def test_rebecca_get_current_admin_logs_in_and_probes_api_admin():
    auth_headers: list[str | None] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "tok", "token_type": "bearer"})
        if req.url.path == "/api/admin" and req.method == "GET":
            auth_headers.append(req.headers.get("Authorization"))
            return httpx.Response(200, json={"username": "admin", "is_sudo": True})
        return httpx.Response(404)

    client = _make_rebecca_client(handler)
    admin = await client.get_current_admin()
    assert admin["username"] == "admin"
    assert auth_headers == ["Bearer tok"]  # auto-login happened first
    await client.aclose()


def test_marzban_family_clients_both_expose_get_current_admin():
    """The provisioning preflight (services/provisioning/manager.py) calls
    get_current_admin() on ANY Marzban-family client — both must have it."""
    from services.pasarguard.client import PasarGuardClient

    assert callable(getattr(RebeccaClient, "get_current_admin", None))
    assert callable(getattr(PasarGuardClient, "get_current_admin", None))
