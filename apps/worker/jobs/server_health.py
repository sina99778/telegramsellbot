"""
Server health check job.

Pings every active X-UI panel. A server only flips to "unhealthy" after a few
consecutive failures (so one transient blip doesn't stop sales), and admins are
alerted ONLY on a state transition — when a server goes down, and again when it
recovers — instead of every run. The server's `health_status` is kept accurate
so the rest of the app (and the sales path) can avoid dead servers.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.xui import XUIServerRecord
from services.xui.runtime import create_xui_client_for_server
from services.xui.client import XUIClientError

logger = logging.getLogger(__name__)

# Consecutive failures before we declare a server down (and stop selling on it).
_FAILURE_STRIKES = 3


async def _probe(server: XUIServerRecord) -> tuple[bool, str]:
    """Return (ok, error_message). Full connectivity = login + list inbounds."""
    try:
        async with create_xui_client_for_server(server) as xui_client:
            inbounds = await xui_client.get_inbounds()
        logger.info("[HEALTH] ✅ %s — OK (%d inbounds)", server.name or server.base_url, len(inbounds))
        return True, ""
    except XUIClientError as exc:
        return False, str(exc)[:200]
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]


async def check_server_health(session: AsyncSession, bot: Bot) -> None:
    result = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(
            XUIServerRecord.is_active.is_(True),
            XUIServerRecord.health_status != "deleted",
        )
    )
    servers = list(result.scalars().all())
    if not servers:
        logger.info("[HEALTH] No active servers to check")
        return

    newly_down: list[tuple[str, str]] = []   # crossed into "unhealthy" this run
    recovered: list[str] = []                # came back up this run

    for server in servers:
        name = server.name or server.base_url
        ok, error = await _probe(server)
        if ok:
            if server.health_status == "unhealthy":
                recovered.append(name)
            server.health_check_failures = 0
            server.health_status = "healthy"
        else:
            server.health_check_failures = int(server.health_check_failures or 0) + 1
            logger.error(
                "[HEALTH] ❌ %s — FAILED (%d/%d): %s",
                name, server.health_check_failures, _FAILURE_STRIKES, error,
            )
            if server.health_check_failures >= _FAILURE_STRIKES and server.health_status != "unhealthy":
                server.health_status = "unhealthy"
                newly_down.append((name, error))

    await session.flush()

    if not newly_down and not recovered:
        return

    from services.notifications import notify_admins

    if newly_down:
        lines = [f"🚨 <b>{len(newly_down)} سرور از کار افتاد</b> (فروش روی آن متوقف شد):\n"]
        for name, error in newly_down:
            lines.append(f"❌ <b>{name}</b>\n   ⚠️ {error}\n")
        lines.append("\nلطفاً هرچه سریع‌تر بررسی کنید.")
        try:
            await notify_admins(session, bot, "\n".join(lines))
        except Exception as exc:
            logger.error("[HEALTH] failed to send down alert: %s", exc)

    if recovered:
        lines = [f"✅ <b>{len(recovered)} سرور دوباره سالم شد</b> (فروش از سر گرفته شد):\n"]
        for name in recovered:
            lines.append(f"🟢 <b>{name}</b>")
        try:
            await notify_admins(session, bot, "\n".join(lines))
        except Exception as exc:
            logger.error("[HEALTH] failed to send recovery alert: %s", exc)
