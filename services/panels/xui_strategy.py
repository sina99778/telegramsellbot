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


def _client_is_gone(error_msg: str) -> bool:
    """The panel ACTIVELY says the client no longer exists (vs a transient blip)."""
    low = error_msg.lower()
    return (
        "no traffic stats found" in low
        or "404" in error_msg
        or "not found" in low  # covers "Inbound Not Found For Email"
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
                if _client_is_gone(str(exc)):
                    return UsageInfo(gone=True)
                raise
        return UsageInfo(used_bytes=traffic.used_bytes)

    async def delete_config(self, *, server: Any, record: Any) -> None:
        inbound = record.inbound
        if inbound is None:
            raise ValueError("X-UI client record has no inbound mapping.")
        ensure_inbound_server_loaded(inbound)
        async with create_xui_client_for_server(server) as client:
            await client.delete_client(
                inbound_id=inbound.xui_inbound_remote_id,
                client_id=record.xui_client_remote_id or record.client_uuid,
            )
