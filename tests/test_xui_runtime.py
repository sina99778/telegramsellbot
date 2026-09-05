from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

from services.xui.runtime import build_sub_link, evict_panel_client, normalize_base_url
import services.xui.runtime as xui_runtime
import services.pasarguard.runtime as pasarguard_runtime
import services.rebecca.runtime as rebecca_runtime


def test_build_sub_link_does_not_require_server_metadata():
    server = SimpleNamespace(
        base_url="https://panel.example.com:54321/path",
        sub_domain=None,
        subscription_port=2096,
    )

    assert build_sub_link(server, "abc123") == "http://panel.example.com:2096/sub/abc123"


def test_build_sub_link_uses_scheme_prefix_from_sub_domain():
    server = SimpleNamespace(
        base_url="https://panel.example.com:54321",
        sub_domain="http://sub.example.com",
        subscription_port=2096,
    )

    assert build_sub_link(server, "abc123") == "http://sub.example.com:2096/sub/abc123"


def test_normalize_base_url_only_normalizes_panel_input():
    assert normalize_base_url(" panel.example.com:54321/// ") == "http://panel.example.com:54321"
    assert normalize_base_url("HTTPS://panel.example.com:54321/") == "HTTPS://panel.example.com:54321"


async def test_evict_panel_client_closes_all_runtime_pool_entries():
    server_id = uuid4()
    server = SimpleNamespace(id=server_id, panel_type="sanaei_xui")
    xui_client = SimpleNamespace(aclose=AsyncMock())
    pg_client = SimpleNamespace(aclose=AsyncMock())
    rebecca_client = SimpleNamespace(aclose=AsyncMock())
    xui_runtime._xui_clients_pool[server_id] = SimpleNamespace(client=xui_client)
    pasarguard_runtime._pg_clients_pool[server_id] = SimpleNamespace(client=pg_client)
    rebecca_runtime._rebecca_clients_pool[server_id] = SimpleNamespace(client=rebecca_client)

    await evict_panel_client(server)

    assert server_id not in xui_runtime._xui_clients_pool
    assert server_id not in pasarguard_runtime._pg_clients_pool
    assert server_id not in rebecca_runtime._rebecca_clients_pool
    xui_client.aclose.assert_awaited_once()
    pg_client.aclose.assert_awaited_once()
    rebecca_client.aclose.assert_awaited_once()
