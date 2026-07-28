"""
One-off DB migration: clear a stored ``xui_limit_ip`` of 1.

Why this exists
---------------
3x-ui's CheckClientIpJob is scheduled ``@every 10s``. It only reaches its
broken ``clearAccessLog()`` when SOME client carries ``limitIp > 0``
(``hasLimitIp()``), and ``clearAccessLog()`` calls ``checkError(err)`` after
``os.Open`` WITHOUT returning — so a failed open hands ``io.Copy`` a nil
``*os.File``, which yields ``os.ErrInvalid``. That is the panel log line:

    WARNING - X-UI: client ip job err:invalid argument

repeated every 10 seconds.

Our bot used to default ``xui_limit_ip`` to 1 and stamp it on every client it
created. That default is now 0, but the default is only a FALLBACK: once an
admin opened the security settings screen and saved, ``xui_limit_ip: 1`` was
persisted into ``app_settings`` and that stored value wins forever. The
running bot would then keep re-asserting limitIp=1 and the spam would survive
the upgrade — which is exactly what was observed.

This migration rewrites a stored 1 to 0 so the new default actually takes
effect. It deliberately only touches the exact value 1 (the old default): an
admin who intentionally configured 2, 3, ... keeps their setting.

The X-UI cap is inert on a normal deploy anyway — enforcement needs both the
Xray access log configured AND fail2ban installed in the panel container, and
upstream now resets these limits to 0 on upgrade. Device limiting is handled
by our own ``auto_disable_ip_abuse`` guard.

Idempotent: re-running is a no-op once the value is 0.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select

from core.database import AsyncSessionFactory
from models.app_setting import AppSetting
from repositories.settings import SERVICE_SECURITY_SETTINGS_KEY


logger = logging.getLogger("006_reset_xui_limit_ip")

# Only the historical default is cleared; a deliberate admin choice is kept.
_LEGACY_DEFAULT = 1


async def _run() -> int:
    async with AsyncSessionFactory() as session:
        record = (
            await session.execute(
                select(AppSetting).where(AppSetting.key == SERVICE_SECURITY_SETTINGS_KEY)
            )
        ).scalar_one_or_none()

        if record is None or not record.value_json:
            logger.info("No stored security settings — the new default (0) already applies.")
            return 0

        payload = dict(record.value_json)
        stored = payload.get("xui_limit_ip")

        if stored is None:
            logger.info("No stored xui_limit_ip — the new default (0) already applies.")
            return 0

        try:
            stored_int = int(stored)
        except (TypeError, ValueError):
            logger.warning("Stored xui_limit_ip is not a number (%r) — leaving it alone.", stored)
            return 0

        if stored_int == 0:
            logger.info("Stored xui_limit_ip is already 0 — nothing to do.")
            return 0

        if stored_int != _LEGACY_DEFAULT:
            logger.info(
                "Stored xui_limit_ip is %s (a deliberate admin choice, not the old default) "
                "— leaving it alone. Note any value > 0 keeps the panel's 10s "
                "'client ip job err' warnings coming.",
                stored_int,
            )
            return 0

        payload["xui_limit_ip"] = 0
        # Reassign (not mutate) so SQLAlchemy reliably flags the JSON column dirty.
        record.value_json = payload
        session.add(record)
        await session.commit()
        logger.info(
            "Reset stored xui_limit_ip %s -> 0. The sync job will now push limitIp=0 "
            "to panel clients, which stops the 'client ip job err' log spam.",
            _LEGACY_DEFAULT,
        )
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        return asyncio.run(_run())
    except Exception as exc:
        logger.error("Migration failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
