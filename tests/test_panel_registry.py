"""Tests for the panel-strategy registry — dispatch, unknown-panel safety,
registration, and delete_config wiring."""
from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

import pytest

from services.panels.base import PanelCapabilities, PanelStrategy
from services.panels.registry import (
    PanelNotRegisteredError,
    is_known_panel_type,
    register_panel,
    strategy_for_panel_type,
    strategy_for_record,
    strategy_for_server,
)


def _cm(client):
    @asynccontextmanager
    async def _factory(server):
        yield client
    return _factory


def test_dispatch_by_panel_type_and_record():
    assert strategy_for_server(NS(panel_type="sanaei_xui")).kind == "xui"
    assert strategy_for_server(NS(panel_type="pasarguard")).kind == "pasarguard"
    assert strategy_for_record(NS(panel_kind=None)).kind == "xui"
    assert strategy_for_record(NS(panel_kind="pasarguard")).kind == "pasarguard"


def test_unknown_panel_raises_not_silently_xui():
    assert not is_known_panel_type("rebka")
    with pytest.raises(PanelNotRegisteredError):
        strategy_for_panel_type("rebka")


def test_register_panel_enables_a_new_kind():
    class RebkaStub:
        kind = "rebka"
        caps = PanelCapabilities(False, False, False, False, False, False)

        async def health_probe(self, server):  # pragma: no cover
            ...

        async def fetch_usage(self, *, server, record):  # pragma: no cover
            ...

        async def delete_config(self, *, server, record):  # pragma: no cover
            ...

    assert isinstance(RebkaStub(), PanelStrategy)  # structurally satisfies the contract
    register_panel("rebka", RebkaStub())
    try:
        assert is_known_panel_type("rebka")
        assert strategy_for_panel_type("rebka").kind == "rebka"
        assert strategy_for_server(NS(panel_type="rebka")).kind == "rebka"
    finally:
        from services.panels import registry as reg
        reg._REGISTRY.pop("rebka", None)


@pytest.mark.asyncio
async def test_pasarguard_delete_dispatches(monkeypatch):
    import services.panels.pasarguard_strategy as pg

    calls: list = []

    class FakePG:
        async def delete_user(self, username):
            calls.append(username)

    monkeypatch.setattr(pg, "create_pasarguard_client_for_server", _cm(FakePG()))
    rec = NS(panel_kind="pasarguard", panel_username="u_abc", username="u_abc")
    await strategy_for_record(rec).delete_config(server=NS(base_url="http://h"), record=rec)
    assert calls == ["u_abc"]


@pytest.mark.asyncio
async def test_xui_delete_dispatches(monkeypatch):
    import services.panels.xui_strategy as xs

    calls: list = []

    class FakeXUI:
        async def delete_client(self, *, inbound_id, client_id):
            calls.append((inbound_id, client_id))

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    inbound = NS(xui_inbound_remote_id=7, server=NS(credentials=NS()))
    rec = NS(panel_kind=None, inbound=inbound, xui_client_remote_id="cid", client_uuid="uuid")
    await strategy_for_record(rec).delete_config(server=inbound.server, record=rec)
    assert calls == [(7, "cid")]
