"""
Marzban-family seam — PasarGuard and Rebecca are both Marzban forks with a
near-identical user-centric API, so they SHARE one code path. This module is the
single place that:
  * tells whether a server/record belongs to the Marzban family,
  * yields the right client (PasarGuard or Rebecca) for a server,
  * provides the shared PanelStrategy (caps + health/usage/delete).

The two clients expose a uniform interface (login, list_bundles,
create_user_in_bundle, get_user, modify_user, delete_user, reset_user_usage,
revoke_sub) so the lifecycle code never branches PasarGuard-vs-Rebecca — only
Marzban-family-vs-X-UI. Adding a third Marzban fork = one client + one registry
line; no lifecycle edits.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from services.panels.base import PanelCapabilities, UsageInfo


PANEL_PASARGUARD = "pasarguard"
PANEL_REBECCA = "rebecca"
MARZBAN_FAMILY = frozenset({PANEL_PASARGUARD, PANEL_REBECCA})


def _norm(value: Any) -> str:
    return (value or "").strip().lower()


def is_marzban_family(server: Any) -> bool:
    return _norm(getattr(server, "panel_type", None)) in MARZBAN_FAMILY


def record_is_marzban_family(record: Any) -> bool:
    return _norm(getattr(record, "panel_kind", None)) in MARZBAN_FAMILY


@asynccontextmanager
async def marzban_client_for_server(server: Any):
    """Yield the Marzban-family client (PasarGuard or Rebecca) for this server,
    dispatched by `panel_type`. Both clients share the uniform interface used by
    the lifecycle code."""
    if _norm(getattr(server, "panel_type", None)) == PANEL_REBECCA:
        from services.rebecca.runtime import create_rebecca_client_for_server
        async with create_rebecca_client_for_server(server) as client:
            yield client
    else:  # pasarguard (the family default)
        from services.pasarguard.runtime import create_pasarguard_client_for_server
        async with create_pasarguard_client_for_server(server) as client:
            yield client


@asynccontextmanager
async def marzban_client_from_credentials(panel_type: str, *, base_url: str, username: str, password: str):
    """Yield a Marzban-family client built from RAW credentials (no server row
    yet) — for the add-server / connection-test flow. Dispatches by panel_type."""
    from pydantic import SecretStr

    from core.config import settings

    if _norm(panel_type) == PANEL_REBECCA:
        from services.rebecca.client import RebeccaClient, RebeccaClientConfig
        cfg = RebeccaClientConfig(
            base_url=base_url, username=username, password=SecretStr(password),
            timeout_seconds=15.0, verify_ssl=settings.rebecca_verify_ssl,
        )
        async with RebeccaClient(cfg) as client:
            yield client
    else:
        from services.pasarguard.client import PasarGuardClient, PasarGuardClientConfig
        cfg = PasarGuardClientConfig(
            base_url=base_url, username=username, password=SecretStr(password),
            timeout_seconds=15.0, verify_ssl=settings.pasarguard_verify_ssl,
        )
        async with PasarGuardClient(cfg) as client:
            yield client


# All Marzban-family panels share the same capability profile.
MARZBAN_CAPS = PanelCapabilities(
    ip_abuse=False,
    uuid_rotation=False,
    xray_restart=False,
    db_backup=False,
    inbound_migration=False,
    has_vless_uri=False,  # ready subscription link, no synthesised vless:// URI
)


class MarzbanFamilyStrategy:
    """One strategy instance per registered Marzban-family kind. All operations
    dispatch the client by server.panel_type via marzban_client_for_server."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.caps = MARZBAN_CAPS

    @staticmethod
    def _username(record: Any) -> str:
        return record.panel_username or record.username

    async def health_probe(self, server: Any) -> None:
        async with marzban_client_for_server(server) as client:
            await client.login()
            await client.list_bundles()

    async def fetch_usage(self, *, server: Any, record: Any) -> UsageInfo:
        async with marzban_client_for_server(server) as client:
            user = await client.get_user(self._username(record))
        if user is None:
            return UsageInfo(gone=True)
        return UsageInfo(
            used_bytes=int(user.used_traffic or 0),
            status=(user.status or "").lower(),
            expire_ts=user.expire_ts,
        )

    async def delete_config(self, *, server: Any, record: Any) -> None:
        async with marzban_client_for_server(server) as client:
            await client.delete_user(self._username(record))
