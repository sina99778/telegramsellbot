"""
Automated backup job — runs every 6 hours.
Backs up:
  1. Bot's PostgreSQL database (pg_dump)
  2. X-UI Sanaei panel databases (via /server/getDb API)
Sends both as files to all admin users via Telegram.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from models.user import User
from models.xui import XUIServerRecord
from services.xui.client import XUIClientError
from services.xui.runtime import create_xui_client_for_server

logger = logging.getLogger(__name__)


async def _get_admin_telegram_ids(session: AsyncSession) -> set[int]:
    ids: set[int] = set()
    if settings.owner_telegram_id:
        ids.add(settings.owner_telegram_id)
    try:
        result = await session.execute(
            select(User.telegram_id).where(User.role.in_(["admin", "owner"]))
        )
        for row in result.scalars().all():
            ids.add(row)
    except Exception as exc:
        logger.warning("Failed to query admin users: %s", exc)
    return ids


async def _dump_postgres() -> bytes | None:
    db_url = settings.database_url
    try:
        clean = db_url.split("://", 1)[1]
        userpass, hostdb = clean.rsplit("@", 1)
        user, password = userpass.split(":", 1)
        hostport, dbname = hostdb.split("/", 1)
        if ":" in hostport:
            host, port = hostport.split(":", 1)
        else:
            host, port = hostport, "5432"
    except (ValueError, IndexError) as exc:
        logger.error("Failed to parse DATABASE_URL for pg_dump: %s", exc)
        return None

    import os
    env = {**os.environ, "PGPASSWORD": password}
    cmd = ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", dbname,
           "--no-owner", "--no-privileges", "-F", "c"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if proc.returncode != 0:
            logger.error("pg_dump failed (code %d): %s", proc.returncode, stderr.decode()[:500])
            return None
        logger.info("pg_dump successful: %d bytes", len(stdout))
        return stdout
    except asyncio.TimeoutError:
        logger.error("pg_dump timed out")
        return None
    except FileNotFoundError:
        logger.error("pg_dump not found — install postgresql-client in container")
        return None
    except Exception as exc:
        logger.error("pg_dump failed: %s", exc)
        return None


async def _dump_xui_databases(session: AsyncSession) -> list[tuple[str, bytes]]:
    result = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.is_active.is_(True), XUIServerRecord.health_status != "deleted")
    )
    servers = list(result.scalars().all())
    backups: list[tuple[str, bytes]] = []
    for server in servers:
        if server.credentials is None:
            continue
        try:
            async with create_xui_client_for_server(server) as xui_client:
                db_bytes = await xui_client.get_db_backup()
                backups.append((server.name, db_bytes))
                logger.info("Downloaded X-UI DB from '%s': %d bytes", server.name, len(db_bytes))
        except (XUIClientError, Exception) as exc:
            logger.error("Failed to download X-UI DB from '%s': %s", server.name, exc)
    return backups


async def run_backup(session: AsyncSession, bot: Bot, manual_requester_id: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M")
    logger.info("[BACKUP] Starting at %s", timestamp)

    if manual_requester_id is not None:
        admin_ids = {manual_requester_id}
    else:
        admin_ids = await _get_admin_telegram_ids(session)
        
    if not admin_ids:
        logger.warning("[BACKUP] No admin IDs — skipping")
        return

    files_to_send: list[BufferedInputFile] = []

    pg_data = await _dump_postgres()
    if pg_data:
        files_to_send.append(BufferedInputFile(pg_data, filename=f"telegramsellbot_{timestamp}.dump"))

    xui_backups = await _dump_xui_databases(session)
    for server_name, db_bytes in xui_backups:
        safe_name = server_name.replace(" ", "_").replace("/", "_")[:30]
        files_to_send.append(BufferedInputFile(db_bytes, filename=f"xui_{safe_name}_{timestamp}.db"))

    if not files_to_send:
        for tg_id in admin_ids:
            try:
                await bot.send_message(tg_id, "⚠️ بکاپ اتوماتیک ناموفق بود.")
            except Exception:
                pass
        return

    type_str = "دستی" if manual_requester_id else "اتوماتیک"
    caption = f"🗄 بکاپ {type_str}\n📅 {now.strftime('%Y-%m-%d %H:%M UTC')}\n📦 {len(files_to_send)} فایل"
    for tg_id in admin_ids:
        try:
            await bot.send_message(tg_id, caption)
            for file in files_to_send:
                await bot.send_document(tg_id, file)
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.error("[BACKUP] Failed to send to admin %s: %s", tg_id, exc)
    logger.info("[BACKUP] Done")
