"""Tests for mapping PasarGuard groups onto XUIInboundRecord rows
(apps/bot/handlers/admin/servers.py::_sync_remote_groups)."""
from __future__ import annotations

from types import SimpleNamespace as NS
from uuid import uuid4

from apps.bot.handlers.admin.servers import _sync_remote_groups


def _group(gid: int, name: str, tags=(), disabled=False):
    return NS(id=gid, name=name, inbound_tags=list(tags), is_disabled=disabled)


def test_creates_rows_for_new_groups():
    created, created_count, disabled = _sync_remote_groups(
        server_id=uuid4(),
        existing_inbounds=[],
        groups=[_group(1, "de", ["vless-tcp"]), _group(2, "fr")],
    )
    assert created_count == 2
    assert disabled == 0
    row = created[0]
    assert row.xui_inbound_remote_id == 1
    assert row.remark == "de" and row.tag == "de"
    assert row.protocol == "pasarguard"
    assert row.port is None
    assert row.is_active is True
    assert row.metadata_["pasarguard_group"] is True
    assert row.metadata_["inbound_tags"] == ["vless-tcp"]


def test_disabled_group_creates_inactive_row():
    created, _c, _d = _sync_remote_groups(
        server_id=uuid4(),
        existing_inbounds=[],
        groups=[_group(5, "x", disabled=True)],
    )
    assert created[0].is_active is False


def test_updates_existing_and_disables_missing():
    existing = [
        NS(xui_inbound_remote_id=1, remark="old", tag=None, protocol=None, port=7, is_active=True, metadata_={}),
        NS(xui_inbound_remote_id=9, remark="gone", tag="gone", protocol="pasarguard", port=None, is_active=True, metadata_={}),
    ]
    created, created_count, disabled = _sync_remote_groups(
        server_id=uuid4(),
        existing_inbounds=existing,
        groups=[_group(1, "de-new", ["x"])],
    )
    # group 1 updated in place, group 9 no longer present -> disabled, no new rows
    assert created_count == 0
    assert disabled == 1
    assert existing[0].remark == "de-new"
    assert existing[0].protocol == "pasarguard"
    assert existing[0].port is None
    assert existing[0].is_active is True
    assert existing[0].metadata_ == {"pasarguard_group": True, "inbound_tags": ["x"]}
    assert existing[1].is_active is False  # missing group disabled
