"""Regression tests for the X-UI high-severity fixes:

#20 — XUIStrategy.delete_config honours the idempotency contract
      (already-gone panel client == successful delete).
#27 — SanaeiXUIClient._send must not blindly re-send non-idempotent POSTs
      (addClient/delClient) after a mid-flight timeout/disconnect.
#28 — build_vless_uri must read 3x-ui's nested REALITY/TLS stream-settings
      shape (realitySettings.settings + plural serverNames/shortIds).
#61 — _client_is_gone must not substring-match transport errors whose message
      embeds the user-chosen email (e.g. a config named 'x404').
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr

import services.panels.xui_strategy as xs
from services.panels.xui_strategy import XUIStrategy, _client_is_gone
from services.xui.client import SanaeiXUIClient, XUIClientConfig, XUIRequestError
from services.xui.runtime import build_vless_uri


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _cm(client):
    @asynccontextmanager
    async def _factory(server):
        yield client
    return _factory


def _make_record():
    inbound = NS(xui_inbound_remote_id=7, server=NS(credentials=NS()))
    return NS(
        panel_kind=None,
        inbound=inbound,
        xui_client_remote_id="cid",
        client_uuid="uuid",
        email="x404_abc123",
    )


def _make_http_client(request_coro):
    http = MagicMock()
    http.request = request_coro
    cfg = XUIClientConfig(base_url="http://panel", username="u", password=SecretStr("p"))
    return SanaeiXUIClient(cfg, http_client=http)


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the retry backoff instantaneous."""
    sleeps: list[float] = []

    async def _fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    return sleeps


# ---------------------------------------------------------------------------
# #61 — _client_is_gone classification
# ---------------------------------------------------------------------------

def test_gone_on_structured_404():
    exc = XUIRequestError(
        "X-UI request to 'panel/api/inbounds/getClientTraffics/foo' failed with status 404: nope",
        status_code=404,
    )
    assert _client_is_gone(exc) is True


def test_gone_on_panel_not_found_messages():
    assert _client_is_gone(XUIRequestError("Inbound Not Found For Email: foo_ab12cd")) is True
    assert _client_is_gone(XUIRequestError("record not found")) is True
    assert _client_is_gone(XUIRequestError("No traffic stats found for client.")) is True


def test_timeout_with_404_in_email_is_not_gone():
    # The user-chosen config name may contain '404' — a pure timeout whose
    # message embeds the request path must never count as "client gone".
    exc = XUIRequestError(
        "Timed out while calling X-UI endpoint 'panel/api/inbounds/getClientTraffics/x404_abc123'."
    )
    assert _client_is_gone(exc) is False


def test_connect_error_with_email_in_path_is_not_gone():
    exc = XUIRequestError(
        "ConnectError while calling X-UI endpoint 'panel/api/inbounds/getClientTraffics/x404_abc123'"
        " (caused by OSError: [Errno 111] Connection refused)"
    )
    assert _client_is_gone(exc) is False


def test_non_404_http_status_is_not_gone():
    exc = XUIRequestError(
        "X-UI request to 'panel/api/inbounds/getClientTraffics/x404_abc123' failed with status 500: boom",
        status_code=500,
    )
    assert _client_is_gone(exc) is False


# ---------------------------------------------------------------------------
# #20 — delete_config idempotency
# ---------------------------------------------------------------------------

async def test_delete_config_treats_already_gone_as_success(monkeypatch):
    class FakeXUI:
        async def delete_client(self, *, inbound_id, client_id):
            raise XUIRequestError(
                "X-UI request to 'panel/api/inbounds/7/delClient/cid' failed with status 404: gone",
                status_code=404,
            )

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    record = _make_record()
    # Must NOT raise — an already-deleted panel client counts as deleted.
    await XUIStrategy().delete_config(server=record.inbound.server, record=record)


async def test_delete_config_treats_panel_not_found_msg_as_success(monkeypatch):
    class FakeXUI:
        async def delete_client(self, *, inbound_id, client_id):
            raise XUIRequestError("record not found")

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    record = _make_record()
    await XUIStrategy().delete_config(server=record.inbound.server, record=record)


async def test_delete_config_reraises_transient_errors(monkeypatch):
    class FakeXUI:
        async def delete_client(self, *, inbound_id, client_id):
            raise XUIRequestError(
                "Timed out while calling X-UI endpoint 'panel/api/inbounds/7/delClient/cid'."
            )

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    record = _make_record()
    with pytest.raises(XUIRequestError):
        await XUIStrategy().delete_config(server=record.inbound.server, record=record)


async def test_delete_config_success_passes_args(monkeypatch):
    calls: list = []

    class FakeXUI:
        async def delete_client(self, *, inbound_id, client_id):
            calls.append((inbound_id, client_id))

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    record = _make_record()
    await XUIStrategy().delete_config(server=record.inbound.server, record=record)
    assert calls == [(7, "cid")]


async def test_fetch_usage_timeout_is_not_gone(monkeypatch):
    """A transient timeout must surface as an error, never as gone=True."""

    class FakeXUI:
        async def get_client_traffic(self, email):
            raise XUIRequestError(
                f"Timed out while calling X-UI endpoint 'panel/api/inbounds/getClientTraffics/{email}'."
            )

    monkeypatch.setattr(xs, "create_xui_client_for_server", _cm(FakeXUI()))
    record = _make_record()
    with pytest.raises(XUIRequestError):
        await XUIStrategy().fetch_usage(server=record.inbound.server, record=record)


# ---------------------------------------------------------------------------
# #27 — _send retry semantics
# ---------------------------------------------------------------------------

async def test_post_read_timeout_is_not_resent(no_sleep):
    calls: list = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path))
        raise httpx.ReadTimeout("panel slow")

    client = _make_http_client(fake_request)
    with pytest.raises(XUIRequestError):
        await client._send("POST", "panel/api/inbounds/addClient")
    assert len(calls) == 1  # no blind re-send of a mutating POST


async def test_post_read_error_is_not_resent(no_sleep):
    calls: list = []

    async def fake_request(method, path, **kwargs):
        calls.append((method, path))
        raise httpx.ReadError("connection dropped mid-response")

    client = _make_http_client(fake_request)
    with pytest.raises(XUIRequestError):
        await client._send("POST", "panel/api/inbounds/7/delClient/cid")
    assert len(calls) == 1


async def test_get_read_timeout_is_retried(no_sleep):
    calls: list = []

    async def fake_request(method, path, **kwargs):
        calls.append(method)
        raise httpx.ReadTimeout("panel slow")

    client = _make_http_client(fake_request)
    with pytest.raises(XUIRequestError):
        await client._send("GET", "panel/api/inbounds/list")
    assert len(calls) == 3  # idempotent GETs keep the full retry budget


async def test_post_connect_error_is_retried(no_sleep):
    """Connection-phase failures (request never reached the panel) stay retryable."""
    ok = MagicMock()
    ok.raise_for_status = MagicMock()
    outcomes = [httpx.ConnectError("refused"), httpx.ConnectTimeout("timed out"), ok]

    async def fake_request(method, path, **kwargs):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client = _make_http_client(fake_request)
    response = await client._send("POST", "panel/api/inbounds/addClient")
    assert response is ok


async def test_explicit_idempotent_post_is_retried(no_sleep):
    calls: list = []

    async def fake_request(method, path, **kwargs):
        calls.append(method)
        raise httpx.ReadTimeout("panel slow")

    client = _make_http_client(fake_request)
    with pytest.raises(XUIRequestError):
        await client._send("POST", "login", idempotent=True)
    assert len(calls) == 3


async def test_http_404_sets_structured_status_code(no_sleep):
    resp = MagicMock()
    resp.status_code = 404
    resp.text = "client not found"

    async def fake_request(method, path, **kwargs):
        out = MagicMock()
        out.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("404", request=MagicMock(), response=resp)
        )
        return out

    client = _make_http_client(fake_request)
    with pytest.raises(XUIRequestError) as excinfo:
        await client._send("POST", "panel/api/inbounds/7/delClient/cid")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# #28 — build_vless_uri REALITY / TLS key shapes
# ---------------------------------------------------------------------------

def _uri_params(uri: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(uri).query).items()}


def _make_server():
    return NS(config_domain="vpn.example.test", base_url="http://1.2.3.4:54321/xui")


def _make_inbound(stream: dict):
    return NS(port=8443, protocol="vless", metadata_={"stream_settings": stream})


def test_reality_uri_reads_nested_3xui_shape():
    stream = {
        "network": "tcp",
        "security": "reality",
        "tcpSettings": {"header": {"type": "none"}},
        "realitySettings": {
            "show": False,
            "xver": 0,
            "dest": "yahoo.com:443",
            "serverNames": ["yahoo.com", "www.yahoo.com"],
            "privateKey": "PRIVKEY",
            "shortIds": ["ab12cd34"],
            "settings": {
                "publicKey": "PUBKEY",
                "fingerprint": "chrome",
                "serverName": "",
                "spiderX": "/",
            },
        },
    }
    uri = build_vless_uri(
        client_uuid="11111111-2222-3333-4444-555555555555",
        server=_make_server(),
        inbound=_make_inbound(stream),
        sub_id="subid",
    )
    params = _uri_params(uri)
    assert params["security"] == "reality"
    assert params["pbk"] == "PUBKEY"
    assert params["sid"] == "ab12cd34"
    assert params["sni"] == "yahoo.com"
    assert params["fp"] == "chrome"
    assert params["spx"] == "/"
    # the private key must never leak into the client URI
    assert "PRIVKEY" not in uri


def test_reality_uri_still_supports_flat_legacy_shape():
    stream = {
        "network": "tcp",
        "security": "reality",
        "realitySettings": {
            "publicKey": "FLATPBK",
            "shortId": "ff00",
            "serverName": "flat.example",
            "fingerprint": "firefox",
        },
    }
    uri = build_vless_uri(
        client_uuid="u-1",
        server=_make_server(),
        inbound=_make_inbound(stream),
        sub_id="subid",
    )
    params = _uri_params(uri)
    assert params["pbk"] == "FLATPBK"
    assert params["sid"] == "ff00"
    assert params["sni"] == "flat.example"
    assert params["fp"] == "firefox"


def test_tls_uri_reads_nested_fingerprint():
    stream = {
        "network": "ws",
        "security": "tls",
        "wsSettings": {"path": "/ws"},
        "tlsSettings": {
            "serverName": "tls.example",
            "alpn": ["h2", "http/1.1"],
            "settings": {"allowInsecure": False, "fingerprint": "chrome"},
        },
    }
    uri = build_vless_uri(
        client_uuid="u-1",
        server=_make_server(),
        inbound=_make_inbound(stream),
        sub_id="subid",
    )
    params = _uri_params(uri)
    assert params["sni"] == "tls.example"
    assert params["fp"] == "chrome"
