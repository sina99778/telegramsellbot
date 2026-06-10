"""
Panel-strategy contract — the seam that lets the bot support N VPN panels
(X-UI, PasarGuard, and next: Rebka) without scattering `if panel == ...` across
the codebase.

A `PanelStrategy` bundles, for one panel kind:
  * `kind`  — the canonical key (e.g. "xui", "pasarguard").
  * `caps`  — what the panel CAN do, so UI/skip logic reads a flag instead of
              hard-coding a panel name.
  * a small set of per-config lifecycle verbs the rest of the app calls.

Adding a panel = implement ONE strategy class + register it (see registry.py).
Strategies wrap the existing low-level clients (services/xui, services/pasarguard)
— the strategy is the dispatch boundary, not a rewrite of those clients.

The verbs intentionally take primitive context (server record, client record,
session) rather than panel-specific types, so call sites are panel-agnostic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PanelCapabilities:
    """What a panel supports. Call sites gate optional features on these flags
    instead of `if is_pasarguard(...)` so a new panel just declares its caps."""

    ip_abuse: bool          # per-client IP listing / anti-share (X-UI clientIps)
    uuid_rotation: bool     # rotate a client UUID in place (else: rotate the sub link)
    xray_restart: bool      # restart the Xray core on the panel
    db_backup: bool         # download the panel's own DB (X-UI SQLite)
    inbound_migration: bool # move a config between inbounds/servers (X-UI concept)
    has_vless_uri: bool     # we synthesise a vless:// URI (X-UI) vs. a ready sub link


@dataclass(slots=True)
class RemoteGroup:
    """A panel-side grouping a plan can target: an X-UI inbound or a PasarGuard
    group. `remote_id` is what we persist in XUIInboundRecord.xui_inbound_remote_id."""

    remote_id: int
    name: str
    is_disabled: bool = False
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UsageInfo:
    """A panel's view of one config's live usage/state.

    `status`/`expire_ts` are populated by user-centric panels (PasarGuard/Rebka)
    that own activation+expiry; X-UI leaves them None (we drive expiry ourselves).
    `gone` is True when the panel actively reports the config no longer exists
    (404 / "no traffic stats found") so callers can apply the strike logic.
    """

    used_bytes: int = 0
    status: str | None = None
    expire_ts: int | None = None
    gone: bool = False


@runtime_checkable
class PanelStrategy(Protocol):
    kind: str
    caps: PanelCapabilities

    async def health_probe(self, server: Any) -> None:
        """Raise if the panel is unreachable / auth fails; return None on OK."""
        ...

    async def fetch_usage(self, *, server: Any, record: Any) -> UsageInfo:
        """Read one config's live usage/state from the panel."""
        ...

    async def delete_config(self, *, server: Any, record: Any) -> None:
        """Remove the config from the panel (idempotent — 404 == already gone)."""
        ...
