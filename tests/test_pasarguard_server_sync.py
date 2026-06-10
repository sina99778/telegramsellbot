"""Tests for mapping Marzban-family bundles (PasarGuard groups / Rebecca
services) onto XUIInboundRecord rows
(apps/bot/handlers/admin/servers.py::_sync_remote_bundles)."""
from __future__ import annotations

from types import SimpleNamespace as NS
from uuid import uuid4

from apps.bot.handlers.admin.servers import _sync_remote_bundles
from services.panels.base import RemoteGroup


def _bundle(rid: int, name: str, tags=(), disabled=False) -> RemoteGroup:
    return RemoteGroup(remote_id=rid, name=name, is_disabled=disabled, tags=list(tags))


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
