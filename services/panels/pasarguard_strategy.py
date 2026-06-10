"""Back-compat shim. PasarGuard now uses the shared MarzbanFamilyStrategy
(services/panels/marzban.py) since PasarGuard and Rebecca are both Marzban forks."""
from __future__ import annotations

from services.panels.marzban import MARZBAN_CAPS as PASARGUARD_CAPS  # noqa: F401
from services.panels.marzban import MarzbanFamilyStrategy


def PasarGuardStrategy() -> MarzbanFamilyStrategy:
    return MarzbanFamilyStrategy("pasarguard")
