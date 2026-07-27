"""Tests for mapping Marzban-family bundles (PasarGuard groups / Rebecca
services) onto XUIInboundRecord rows
(apps/bot/handlers/admin/servers.py::_sync_remote_bundles)."""
from __future__ import annotations

from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

import apps.api.routes.dashboard.servers as dashboard_servers
from apps.api.routes.dashboard.servers import ServerCreateBody, create_server
from apps.bot.handlers.admin.servers import _sync_remote_bundles
from services.panels.base import RemoteGroup


def _bundle(rid: int, name: str, tags=(), disabled=False) -> RemoteGroup:
    return RemoteGroup(remote_id=rid, name=name, is_disabled=disabled, tags=list(tags))


def test_dashboard_server_panel_type_is_canonicalized():
    body = ServerCreateBody(
        name="pg",
        base_url="http://panel.local",
        panel_username="admin",
        panel_password="pw",
        panel_type="  PaSaRGuArD ",
    )
    assert body.panel_type == "pasarguard"


def test_creates_rows_for_new_bundles():
    created, created_count, disabled = _sync_remote_bundles(
        server_id=uuid4(),
        existing_inbounds=[],
        bundles=[_bundle(1, "de", ["vless-tcp"]), _bundle(2, "fr")],
        panel_kind="pasarguard",
    )
    assert created_count == 2
    assert disabled == 0
    row = created[0]
    assert row.xui_inbound_remote_id == 1
    assert row.remark == "de" and row.tag == "de"
    assert row.protocol == "pasarguard"
    assert row.port is None
    assert row.is_active is True
    assert row.metadata_["marzban_bundle"] is True
    assert row.metadata_["inbound_tags"] == ["vless-tcp"]


def test_rebecca_panel_kind_sets_protocol():
    created, _c, _d = _sync_remote_bundles(
        server_id=uuid4(),
        existing_inbounds=[],
        bundles=[_bundle(7, "service-a")],
        panel_kind="rebecca",
    )
    assert created[0].protocol == "rebecca"


def test_disabled_bundle_creates_inactive_row():
    created, _c, _d = _sync_remote_bundles(
        server_id=uuid4(),
        existing_inbounds=[],
        bundles=[_bundle(5, "x", disabled=True)],
        panel_kind="pasarguard",
    )
    assert created[0].is_active is False


def test_updates_existing_and_disables_missing():
    existing = [
        NS(xui_inbound_remote_id=1, remark="old", tag=None, protocol=None, port=7, is_active=True, metadata_={}),
        NS(xui_inbound_remote_id=9, remark="gone", tag="gone", protocol="pasarguard", port=None, is_active=True, metadata_={}),
    ]
    created, created_count, disabled = _sync_remote_bundles(
        server_id=uuid4(),
        existing_inbounds=existing,
        bundles=[_bundle(1, "de-new", ["x"])],
        panel_kind="pasarguard",
    )
    assert created_count == 0
    assert disabled == 1
    assert existing[0].remark == "de-new"
    assert existing[0].protocol == "pasarguard"
    assert existing[0].port is None
    assert existing[0].is_active is True
    assert existing[0].metadata_ == {"marzban_bundle": True, "inbound_tags": ["x"]}
    assert existing[1].is_active is False


@pytest.mark.asyncio
async def test_create_pasarguard_probes_before_commit_and_persists_bundles():
    session = AsyncMock()
    session.add = MagicMock()
    session.add_all = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    async def flush():
        added = session.add.call_args.args[0]
        if getattr(added, "id", None) is None:
            added.id = uuid4()

    session.flush = AsyncMock(side_effect=flush)
    session.commit = AsyncMock()
    admin = NS(username="admin")
    bundles = [_bundle(4, "service")]

    with patch("apps.bot.handlers.admin.servers._fetch_remote_bundles", new=AsyncMock(return_value=bundles)), \
         patch("apps.api.routes.dashboard.servers.AuditLogRepository") as audit_cls, \
         patch("apps.api.routes.dashboard.servers.encrypt_secret", return_value="encrypted"):
        audit_cls.return_value.log_action = AsyncMock()
        result = await create_server(
            ServerCreateBody(
                name="pg",
                base_url="http://panel.local",
                panel_username="admin",
                panel_password="pw",
                panel_type="pasarguard",
            ),
            (admin, session),
        )

    server = session.add.call_args_list[0].args[0]
    assert server.subscription_port == 0
    assert server.health_status == "ok"
    session.commit.assert_awaited_once()
    session.add_all.assert_called_once()
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_test_connection_syncs_marzban_bundles():
    server = NS(
        id=uuid4(), panel_type="rebecca", base_url="http://panel.local",
        credentials=NS(username="admin", password_encrypted="encrypted"),
        inbounds=[NS(xui_inbound_remote_id=1, remark="old", tag="old", protocol="rebecca", port=None, is_active=True, metadata_={})],
        health_status="error",
    )
    session = AsyncMock()
    session.scalar = AsyncMock(return_value=server)
    session.add_all = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    client = NS(login=AsyncMock(), list_bundles=AsyncMock(return_value=[_bundle(1, "new"), _bundle(2, "added")]))

    class ClientContext:
        async def __aenter__(self):
            return client
        async def __aexit__(self, *args):
            return False

    with patch("apps.api.routes.dashboard.servers.decrypt_secret", return_value="pw"), \
         patch("services.panels.marzban.marzban_client_from_credentials", return_value=ClientContext()):
        result = await dashboard_servers.test_connection(
            server.id, (NS(username="admin"), session)
        )

    assert result == {"ok": True, "inbound_count": 2}
    assert server.health_status == "ok"
    assert server.inbounds[0].remark == "new"
    session.add_all.assert_called_once()
    session.commit.assert_awaited_once()
