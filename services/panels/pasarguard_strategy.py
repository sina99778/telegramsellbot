"""PasarGuard panel strategy — wraps services/pasarguard around the PanelStrategy
contract. Adding a panel means writing a sibling of this file."""
from __future__ import annotations

from typing import Any

from services.panels.base import PanelCapabilities, UsageInfo
from services.pasarguard.runtime import create_pasarguard_client_for_server


PASARGUARD_CAPS = PanelCapabilities(
    ip_abuse=False,
    uuid_rotation=False,
    xray_restart=False,
    db_backup=False,
    inbound_migration=False,
    has_vless_uri=False,  # PasarGuard returns a ready subscription link
)


class PasarGuardStrategy:
    kind = "pasarguard"
    caps = PASARGUARD_CAPS

    @staticmethod
    def _username(record: Any) -> str:
        return record.panel_username or record.username

    async def health_probe(self, server: Any) -> None:
        async with create_pasarguard_client_for_server(server) as client:
            await client.login()
            await client.get_groups()

    async def fetch_usage(self, *, server: Any, record: Any) -> UsageInfo:
        async with create_pasarguard_client_for_server(server) as client:
            pg_user = await client.get_user(self._username(record))
        if pg_user is None:
            return UsageInfo(gone=True)
        return UsageInfo(
            used_bytes=int(pg_user.used_traffic or 0),
            status=(pg_user.status or "").lower(),
            expire_ts=pg_user.expire_ts,
        )

    async def delete_config(self, *, server: Any, record: Any) -> None:
        async with create_pasarguard_client_for_server(server) as client:
            await client.delete_user(self._username(record))
