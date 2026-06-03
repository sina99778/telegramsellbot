"""Tests for the panel dispatch seam (services/panels/adapter.py)."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from services.panels.adapter import (
    PANEL_PASARGUARD,
    PANEL_XUI,
    capabilities_for,
    get_panel_adapter,
    is_pasarguard,
    panel_kind_of,
    record_is_pasarguard,
)


@pytest.mark.parametrize(
    "panel_type,expected",
    [
        (None, PANEL_XUI),
        ("", PANEL_XUI),
        ("sanaei_xui", PANEL_XUI),
        ("alireza_xui", PANEL_XUI),  # any unknown X-UI flavor
        ("pasarguard", PANEL_PASARGUARD),
        ("PASARGUARD", PANEL_PASARGUARD),  # case-insensitive
    ],
)
def test_panel_kind_of(panel_type, expected):
    assert panel_kind_of(NS(panel_type=panel_type)) == expected


def test_is_pasarguard():
    assert is_pasarguard(NS(panel_type="pasarguard"))
    assert not is_pasarguard(NS(panel_type="sanaei_xui"))
    assert not is_pasarguard(NS(panel_type=None))


@pytest.mark.parametrize(
    "panel_kind,expected",
    [(None, False), ("xui", False), ("pasarguard", True), ("PasarGuard", True)],
)
def test_record_is_pasarguard(panel_kind, expected):
    assert record_is_pasarguard(NS(panel_kind=panel_kind)) is expected


def test_pasarguard_disables_xui_only_capabilities():
    caps = capabilities_for(NS(panel_type="pasarguard"))
    assert not caps.ip_abuse
    assert not caps.uuid_rotation
    assert not caps.xray_restart
    assert not caps.db_backup
    assert not caps.inbound_migration


def test_xui_keeps_all_capabilities():
    caps = capabilities_for(NS(panel_type="sanaei_xui"))
    assert caps.ip_abuse
    assert caps.uuid_rotation
    assert caps.xray_restart
    assert caps.db_backup
    assert caps.inbound_migration


def test_get_panel_adapter():
    pg = get_panel_adapter(NS(panel_type="pasarguard"))
    assert pg.is_pasarguard and not pg.is_xui and pg.kind == PANEL_PASARGUARD
    xui = get_panel_adapter(NS(panel_type=None))
    assert xui.is_xui and not xui.is_pasarguard and xui.kind == PANEL_XUI
