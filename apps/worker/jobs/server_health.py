"""
Server health check job.
Periodically checks connectivity to all active X-UI panels
and notifies admins if any server is unreachable.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.xui import XUIServerRecord, XUIServerCredential
from services.xui.runtime import create_xui_client_for_server
from services.xui.client import XUIClientError

logger = logging.getLogger(__name__)


async def check_server_health(session: AsyncSession, bot: Bot) -> None:
    """Check connectivity to all active X-UI servers and notify admins on failure."""
    result = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.is_active.is_(True))
    )
    servers = list(result.scalars().all())

    if not servers:
        logger.info("[HEALTH] No active servers to check")
        return

    failed_servers: list[tuple[str, str]] = []  # (name, error)

    for server in servers:
        server_name = server.name or server.base_url
        try:
            async with create_xui_client_for_server(server) as xui_client:
                # Try to login and fetch inbounds — proves full connectivity
                inbounds = await xui_client.get_inbounds()
                logger.info(
                    "[HEALTH] ✅ %s — OK (%d inbounds)",
                    server_name, len(inbounds),
                )
        except XUIClientError as exc:
            error_msg = str(exc)[:200]
            logger.error("[HEALTH] ❌ %s — FAILED: %s", server_name, error_msg)
            failed_servers.append((server_name, error_msg))
        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"[:200]
            logger.error("[HEALTH] ❌ %s — FAILED: %s", server_name, error_msg)
            failed_servers.append((server_name, error_msg))

    if not failed_servers:
        logger.info("[HEALTH] All %d servers are healthy", len(servers))
        return

    # Send alert to admin
    lines = [
        f"🚨 هشدار: {len(failed_servers)} سرور از {len(servers)} سرور غیرقابل دسترسی!\n"
    ]
    for name, error in failed_servers:
        lines.append(f"❌ {name}\n   ⚠️ {error}\n")

    lines.append("\nلطفاً وضعیت سرورها را بررسی کنید.")
    alert_text = "\n".join(lines)

    from services.notifications import notify_admins
    try:
        await notify_admins(session, bot, alert_text)
        logger.info("[HEALTH] Alert sent to admins")
    except Exception as exc:
        logger.error("[HEALTH] Failed to send health alert: %s", exc)
