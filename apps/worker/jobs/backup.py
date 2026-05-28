"""
Scheduled backup job — runs every 6 hours (see apps/worker/main.py).

What got rewritten
------------------
Previous version produced multiple `.dump.enc` / `.db.enc` files
encrypted with the bot's Fernet key. The operator decided that the
extra encryption layer wasn't worth the friction (they keep their
backup channel private already), so this version produces ONE
plain-text `.tar.gz` bundle per cycle — same shape as `backup.sh` —
and sends it as a single Telegram document.

Bundle contents (identical to backup.sh format_version=2):
    db.sql.gz              — gzipped pg_dump
    env                    — .env (renamed so a careless `source` doesn't run it)
    ready_configs/         — operator-uploaded ready configs (if present)
    xui_databases/<srv>.db — each active X-UI panel's database
    manifest.json          — timestamp + git sha + content flags

Delivery:
  * `manual_requester_id` set    → just send to that one admin.
  * sales_report_chat_id set     → send there (operator's archive channel).
  * fall back                    → DM every admin/owner User.
"""
from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import os
import re
import tarfile
import time
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.types import BufferedInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from core.config import settings
from models.user import User
from models.xui import XUIServerRecord
from repositories.settings import AppSettingsRepository
from services.xui.client import XUIClientError
from services.xui.runtime import create_xui_client_for_server


logger = logging.getLogger(__name__)


# Hard cap for the bundle delivered via the Bot API. Telegram's public
# Bot API limits send_document at 50 MB; we leave 2 MB headroom for
# protocol overhead. If a bundle is larger we'd need a local Bot API
# server (or shell out to `tg-spammer` / scp). For most deployments
# the bundle is well under 30 MB so this is just a safety net.
_MAX_BUNDLE_BYTES = 48 * 1024 * 1024


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
    """Run pg_dump for the bot DB and return its bytes."""
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

    env = {**os.environ, "PGPASSWORD": password}
    # Plain SQL format (-F p) so the dump can be restored with `psql`,
    # which is what restore.sh expects. gzip it ourselves for the bundle.
    cmd = ["pg_dump", "-h", host, "-p", port, "-U", user, "-d", dbname,
           "--no-owner", "--no-privileges", "-F", "p"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        if proc.returncode != 0:
            logger.error("pg_dump failed (code %d): %s", proc.returncode, stderr.decode()[:500])
            return None
        logger.info("pg_dump successful: %d bytes", len(stdout))
        return stdout
    except asyncio.TimeoutError:
        logger.error("pg_dump timed out")
        return None
    except FileNotFoundError:
        logger.error("pg_dump not found — install postgresql-client in the container")
        return None
    except Exception as exc:
        logger.error("pg_dump failed: %s", exc)
        return None


async def _dump_xui_databases(session: AsyncSession) -> list[tuple[str, bytes]]:
    """Best-effort: pull each active X-UI panel's DB. Failures don't abort."""
    result = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(
            XUIServerRecord.is_active.is_(True),
            XUIServerRecord.health_status != "deleted",
        )
    )
    servers = list(result.scalars().all())
    dumps: list[tuple[str, bytes]] = []
    for server in servers:
        if server.credentials is None:
            continue
        try:
            async with create_xui_client_for_server(server) as xui_client:
                blob = await xui_client.get_db_backup()
                dumps.append((server.name, blob))
                logger.info("X-UI dump '%s': %d bytes", server.name, len(blob))
        except (XUIClientError, Exception) as exc:
            logger.error("X-UI dump '%s' failed: %s", server.name, exc)
    return dumps


def _read_env_file() -> bytes | None:
    """Read the bot's .env (from inside the container — typically at /app/.env)."""
    for candidate in ("/app/.env", os.path.join(os.getcwd(), ".env"), ".env"):
        try:
            if os.path.isfile(candidate):
                with open(candidate, "rb") as fh:
                    return fh.read()
        except Exception:
            continue
    return None


def _read_ready_configs_dir() -> list[tuple[str, bytes]] | None:
    """If ready_configs/ exists in the project dir, return (relpath, bytes) pairs."""
    base = None
    for candidate in ("/app/ready_configs", os.path.join(os.getcwd(), "ready_configs")):
        if os.path.isdir(candidate):
            base = candidate
            break
    if base is None:
        return None
    out: list[tuple[str, bytes]] = []
    for root, _dirs, files in os.walk(base):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, base)
            try:
                with open(full, "rb") as fh:
                    out.append((rel, fh.read()))
            except Exception as exc:
                logger.warning("ready_configs read fail %s: %s", full, exc)
    return out


def _safe_xui_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:50] or "server"


def _build_bundle(
    *,
    pg_dump_bytes: bytes,
    env_bytes: bytes | None,
    ready_configs: list[tuple[str, bytes]] | None,
    xui_dumps: list[tuple[str, bytes]],
    git_sha: str,
    git_branch: str,
    hostname: str,
) -> bytes:
    """Pack everything into a single in-memory .tar.gz and return its bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # db.sql.gz
        gz = io.BytesIO()
        with gzip.GzipFile(fileobj=gz, mode="wb", filename="db.sql") as gzf:
            gzf.write(pg_dump_bytes)
        gz_bytes = gz.getvalue()
        info = tarfile.TarInfo(name="db.sql.gz"); info.size = len(gz_bytes); info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(gz_bytes))

        # env
        if env_bytes is not None:
            info = tarfile.TarInfo(name="env"); info.size = len(env_bytes); info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(env_bytes))

        # ready_configs/
        if ready_configs:
            for rel, blob in ready_configs:
                info = tarfile.TarInfo(name=f"ready_configs/{rel}"); info.size = len(blob); info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(blob))

        # xui_databases/
        for srv_name, blob in xui_dumps:
            safe = _safe_xui_name(srv_name)
            info = tarfile.TarInfo(name=f"xui_databases/{safe}.db"); info.size = len(blob); info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(blob))

        # manifest.json
        manifest = {
            "format_version": 2,
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": hostname,
            "git_branch": git_branch,
            "git_sha": git_sha,
            "encrypted": False,
            "contents": {
                "db_dump": True,
                "env": env_bytes is not None,
                "ready_configs": bool(ready_configs),
                "xui_databases_count": len(xui_dumps),
            },
        }
        m_bytes = json.dumps(manifest, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name="manifest.json"); info.size = len(m_bytes); info.mtime = int(time.time())
        tar.addfile(info, io.BytesIO(m_bytes))

    return buf.getvalue()


async def run_backup(
    session: AsyncSession,
    bot: Bot,
    manual_requester_id: int | None = None,
) -> None:
    """Build + send a single comprehensive backup bundle.

    Routing:
      * If `manual_requester_id` is set (admin pressed the "Backup now"
        button), send to that one chat.
      * Else if the operator configured a sales-report channel, send
        there (the channel doubles as a backup archive).
      * Else DM every admin/owner User.
    """
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    logger.info("[BACKUP] Starting at %s (manual=%s)", ts, bool(manual_requester_id))

    pg_data = await _dump_postgres()
    if not pg_data:
        # Surface the failure so admins know to fix it.
        targets: set[int] = set()
        if manual_requester_id is not None:
            targets.add(manual_requester_id)
        else:
            targets = await _get_admin_telegram_ids(session)
        for tg_id in targets:
            try:
                await bot.send_message(tg_id, "⚠️ بکاپ خودکار ناموفق بود — pg_dump خطا داد.")
            except Exception:
                pass
        return

    env_data = _read_env_file()
    ready_data = _read_ready_configs_dir()
    xui_data = await _dump_xui_databases(session)

    bundle = _build_bundle(
        pg_dump_bytes=pg_data,
        env_bytes=env_data,
        ready_configs=ready_data,
        xui_dumps=xui_data,
        git_sha=_run_git_sha(),
        git_branch=_run_git_branch(),
        hostname=os.uname().nodename if hasattr(os, "uname") else "host",
    )

    size = len(bundle)
    logger.info("[BACKUP] Bundle built: %d bytes (~%d MB)", size, size // (1024 * 1024))
    if size > _MAX_BUNDLE_BYTES:
        # Surface to ops; don't blow up.
        msg = (
            f"⚠️ بکاپ ساخته شد ({size // (1024*1024)} MB) ولی از سقف Telegram "
            f"({_MAX_BUNDLE_BYTES // (1024*1024)} MB) بزرگ‌تر است.\n"
            "از منوی نصب → Migration Bundle برای انتقال دستی استفاده کن."
        )
        for tg_id in (manual_requester_id and {manual_requester_id}) or (await _get_admin_telegram_ids(session)):
            try:
                await bot.send_message(tg_id, msg)
            except Exception:
                pass
        return

    fname = f"tsb_backup_{ts}.tar.gz"
    doc = BufferedInputFile(bundle, filename=fname)
    caption_lines = [
        f"🗄 بکاپ {'دستی' if manual_requester_id else 'خودکار'}",
        f"📅 {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"💾 حجم: {size // 1024} KB",
        f"📦 شامل: DB" + (" + .env" if env_data else "") + (f" + {len(xui_data)} پنل X-UI" if xui_data else ""),
        "",
        "ℹ️ این فایل رمز نشده. روی هر سرور با ./restore.sh قابل بازگردانی است.",
    ]
    caption = "\n".join(caption_lines)

    # Build target list.
    targets: list[int] = []
    if manual_requester_id is not None:
        targets = [manual_requester_id]
    else:
        try:
            sales_chat_id = await AppSettingsRepository(session).get_sales_report_chat_id()
        except Exception:
            sales_chat_id = None
        if sales_chat_id is not None:
            targets = [sales_chat_id]
        else:
            targets = list(await _get_admin_telegram_ids(session))

    if not targets:
        logger.warning("[BACKUP] No backup targets configured — bundle not delivered.")
        return

    for tg_id in targets:
        try:
            await bot.send_document(tg_id, doc, caption=caption)
        except Exception as exc:
            logger.error("[BACKUP] send_document failed for %s: %s", tg_id, exc)
            try:
                await bot.send_message(tg_id, f"⚠️ ارسال بکاپ شکست خورد: {exc}")
            except Exception:
                pass

    logger.info("[BACKUP] Done — delivered to %d target(s)", len(targets))


# ─── tiny helpers ────────────────────────────────────────────────────────


def _run_git_sha() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd="/app", stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _run_git_branch() -> str:
    try:
        import subprocess
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd="/app", stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"
