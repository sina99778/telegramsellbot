"""
Panel dispatch predicates + capability lookup.

Capabilities now come from the per-panel PanelStrategy (services/panels/registry
+ *_strategy.py) — the single source of truth — so a new panel declares its caps
once. The boolean predicates (is_pasarguard / record_is_pasarguard / panel_kind_of)
remain for the existing `if pasarguard … else <X-UI>` call sites; new code should
prefer the registry (strategy_for_server / strategy_for_record).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.panels.base import PanelCapabilities
from services.panels.marzban import is_marzban_family, record_is_marzban_family  # noqa: F401  (re-exported)
from services.panels.registry import PanelNotRegisteredError, strategy_for_server


PANEL_XUI = "xui"
PANEL_PASARGUARD = "pasarguard"


def panel_kind_of(server: Any) -> str:
    """Return the panel kind for a server record ("xui" | "pasarguard").
    Anything that isn't explicitly "pasarguard" is treated as X-UI."""
    raw = (getattr(server, "panel_type", None) or "").strip().lower()
    return PANEL_PASARGUARD if raw == PANEL_PASARGUARD else PANEL_XUI


def is_pasarguard(server: Any) -> bool:
    return panel_kind_of(server) == PANEL_PASARGUARD


def record_is_pasarguard(record: Any) -> bool:
    """True when an XUIClientRecord row belongs to a PasarGuard panel.
    Cheap per-row check (no server join) used in renewal/usage/my_configs."""
    return (getattr(record, "panel_kind", None) or "").strip().lower() == PANEL_PASARGUARD


def capabilities_for(server: Any) -> PanelCapabilities:
    """The panel's capability flags, sourced from its registered strategy.
    Falls back to X-UI caps for an unregistered type (the lifecycle call would
    then fail at the panel API anyway — see registry's loud-failure note)."""
    try:
        return strategy_for_server(server).caps
    except PanelNotRegisteredError:
        from services.panels.xui_strategy import XUI_CAPS
        return XUI_CAPS


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
    return PanelAdapter(kind=panel_kind_of(server), caps=capabilities_for(server))
