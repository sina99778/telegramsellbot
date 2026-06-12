"""X-UI / Sanaei panel strategy — wraps services/xui around the PanelStrategy
contract. This is the historical default panel; the strategy is a thin
call-through to the existing client so behaviour is byte-for-byte unchanged."""
from __future__ import annotations

from typing import Any

from services.panels.base import PanelCapabilities, UsageInfo
from services.xui.client import XUIRequestError
from services.xui.runtime import create_xui_client_for_server, ensure_inbound_server_loaded


XUI_CAPS = PanelCapabilities(
    ip_abuse=True,
    uuid_rotation=True,
    xray_restart=True,
    db_backup=True,
    inbound_migration=True,
    has_vless_uri=True,
)


def _client_is_gone(exc: Exception) -> bool:
    """The panel ACTIVELY says the client no longer exists (vs a transient blip)."""
    # Structured check first: an HTTP 404 answered by the panel is definitive
    # (XUIRequestError.status_code) — no substring matching on the message.
    if getattr(exc, "status_code", None) == 404:
        return True
    low = str(exc).lower()
    # Transport errors (timeouts / connection failures) embed the request path
    # — which contains the user-chosen email — in the message. Never classify
    # those as "gone": a config named e.g. 'x404' must not match.
    if "while calling x-ui endpoint" in low:
        return False
    return (
        "no traffic stats found" in low
        or "not found" in low  # covers "Inbound Not Found For Email" / gorm "record not found"
    )


class XUIStrategy:
    kind = "xui"
    caps = XUI_CAPS

    async def health_probe(self, server: Any) -> None:
        async with create_xui_client_for_server(server) as client:
            await client.get_inbounds()

    async def fetch_usage(self, *, server: Any, record: Any) -> UsageInfo:
        async with create_xui_client_for_server(server) as client:
            try:
                traffic = await client.get_client_traffic(record.email)
            except XUIRequestError as exc:
                if _client_is_gone(exc):
                    return UsageInfo(gone=True)
                raise
        return UsageInfo(used_bytes=traffic.used_bytes)

    async def delete_config(self, *, server: Any, record: Any) -> None:
        inbound = record.inbound
        if inbound is None:
            raise ValueError("X-UI client record has no inbound mapping.")
        ensure_inbound_server_loaded(inbound)
        async with create_xui_client_for_server(server) as client:
            try:
                await client.delete_client(
                    inbound_id=inbound.xui_inbound_remote_id,
                    client_id=record.xui_client_remote_id or record.client_uuid,
                )
            except XUIRequestError as exc:
                # Contract (base.py): delete is idempotent — an already-gone
                # client counts as a successful delete, so refund flows that
                # gate on "panel delete succeeded" are never blocked.
                if _client_is_gone(exc):
                    return
                raise
