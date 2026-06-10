"""
Panel registry — maps a server's `panel_type` (and a config's `panel_kind`) to
its PanelStrategy. THIS is the seam a new panel plugs into: register one entry.

Crucial safety property: an UNKNOWN non-empty panel_type raises instead of being
silently treated as X-UI. Combined with `is_known_panel_type()` gating the
add-server / dashboard create flows, a server for an unregistered panel (e.g.
"rebka" before its strategy ships) can never be created — and if one somehow
exists, lifecycle code fails loudly rather than driving it with the wrong client.
"""
from __future__ import annotations

from typing import Any

from services.panels.base import PanelStrategy
from services.panels.pasarguard_strategy import PasarGuardStrategy
from services.panels.xui_strategy import XUIStrategy


class PanelNotRegisteredError(Exception):
    """Raised when a server's panel_type has no registered strategy."""


# Stateless singletons.
_XUI = XUIStrategy()
_PASARGUARD = PasarGuardStrategy()

# panel_type / panel_kind string -> strategy. X-UI is the historical default:
# empty/None and every X-UI flavour map to the one X-UI strategy. To add a
# panel, register ONE entry (e.g. register_panel("rebka", RebkaStrategy())).
_REGISTRY: dict[str, PanelStrategy] = {
    "": _XUI,
    "xui": _XUI,
    "sanaei_xui": _XUI,
    "alireza_xui": _XUI,
    "pasarguard": _PASARGUARD,
}


def register_panel(panel_type: str, strategy: PanelStrategy) -> None:
    _REGISTRY[(panel_type or "").strip().lower()] = strategy


def _norm(panel_type: Any) -> str:
    return (panel_type or "").strip().lower()


def known_panel_types() -> set[str]:
    """The non-empty panel_type keys an operator may pick (for add-server /
    dashboard validation). Registering a strategy auto-enables its choice."""
    return {k for k in _REGISTRY if k}


def is_known_panel_type(panel_type: Any) -> bool:
    return _norm(panel_type) in _REGISTRY


def strategy_for_panel_type(panel_type: Any) -> PanelStrategy:
    key = _norm(panel_type)
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise PanelNotRegisteredError(
            f"No panel strategy registered for panel_type={panel_type!r}."
        ) from exc


def strategy_for_server(server: Any) -> PanelStrategy:
    return strategy_for_panel_type(getattr(server, "panel_type", None))


def strategy_for_record(record: Any) -> PanelStrategy:
    """Resolve via the per-row panel_kind (NULL/"xui" => X-UI, "pasarguard" => PG)."""
    return strategy_for_panel_type(getattr(record, "panel_kind", None) or "xui")
