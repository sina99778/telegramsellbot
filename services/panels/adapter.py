"""
Panel dispatch seam — the ONE place that decides whether a server/config talks
to an X-UI panel or a PasarGuard panel.

Design: every lifecycle call site stays `if is_pasarguard(...) → <new PG path>
else → <existing X-UI code, verbatim>`. This module only provides the predicate
+ capability flags; the actual PasarGuard operations live in
services/pasarguard/. Keeping the X-UI branch untouched is the whole point —
existing X-UI rows have `panel_type`/`panel_kind` NULL and always take the else.

Capability flags name the X-UI-only features PasarGuard cannot do, so callers
skip them cleanly instead of erroring:
- ip_abuse:         per-client IP listing / anti-share (X-UI clientIps)
- uuid_rotation:    rotate a client UUID (PasarGuard uses revoke_sub instead)
- xray_restart:     restart the Xray core on the panel
- db_backup:        download the panel's SQLite DB (PasarGuard is Postgres-backed)
- inbound_migration: move a config between inbounds (X-UI concept)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PANEL_XUI = "xui"
PANEL_PASARGUARD = "pasarguard"


def panel_kind_of(server: Any) -> str:
    """Return the panel kind for a server record ("xui" | "pasarguard").
    Anything that isn't explicitly "pasarguard" defaults to X-UI."""
    raw = (getattr(server, "panel_type", None) or "").strip().lower()
    return PANEL_PASARGUARD if raw == PANEL_PASARGUARD else PANEL_XUI


def is_pasarguard(server: Any) -> bool:
    return panel_kind_of(server) == PANEL_PASARGUARD


def record_is_pasarguard(record: Any) -> bool:
    """True when an XUIClientRecord row belongs to a PasarGuard panel.
    Cheap per-row check (no server join) used in renewal/usage/my_configs."""
    return (getattr(record, "panel_kind", None) or "").strip().lower() == PANEL_PASARGUARD


@dataclass(frozen=True, slots=True)
class PanelCapabilities:
    ip_abuse: bool
    uuid_rotation: bool
    xray_restart: bool
    db_backup: bool
    inbound_migration: bool


_XUI_CAPS = PanelCapabilities(
    ip_abuse=True,
    uuid_rotation=True,
    xray_restart=True,
    db_backup=True,
    inbound_migration=True,
)
_PASARGUARD_CAPS = PanelCapabilities(
    ip_abuse=False,
    uuid_rotation=False,
    xray_restart=False,
    db_backup=False,
    inbound_migration=False,
)


def capabilities_for(server: Any) -> PanelCapabilities:
    return _PASARGUARD_CAPS if is_pasarguard(server) else _XUI_CAPS


@dataclass(frozen=True, slots=True)
class PanelAdapter:
    """Ergonomic bundle for a call site: `a = get_panel_adapter(server)` then
    `a.is_pasarguard` / `a.caps.ip_abuse`."""

    kind: str
    caps: PanelCapabilities

    @property
    def is_pasarguard(self) -> bool:
        return self.kind == PANEL_PASARGUARD

    @property
    def is_xui(self) -> bool:
        return self.kind == PANEL_XUI


def get_panel_adapter(server: Any) -> PanelAdapter:
    kind = panel_kind_of(server)
    return PanelAdapter(kind=kind, caps=capabilities_for(server))
