"""
Diagnostic and connection testing service for panel servers.

Provides multi-stage connectivity checks:
1. URL and credential integrity
2. DNS resolution (hostname -> IP)
3. TCP socket connectivity & ping latency (ms)
4. TLS/SSL certificate verification
5. Panel API authentication and strategy health probe
"""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.xui import XUIServerRecord
from services.panels.registry import strategy_for_server

logger = logging.getLogger(__name__)


@dataclass
class ServerDiagnosticResult:
    server_id: UUID
    server_name: str
    server_type: str
    base_url: str
    is_ok: bool
    latency_ms: float | None = None
    resolved_ip: str | None = None
    port: int | None = None
    error_stage: str | None = None  # "CONFIG", "DNS", "TCP", "SSL", "AUTH", "API", "TIMEOUT"
    error_title: str | None = None
    error_detail: str | None = None
    recommendation: str | None = None
    inbounds_count: int | None = None


async def diagnose_server(
    server: XUIServerRecord,
    *,
    timeout: float = 6.0,
) -> ServerDiagnosticResult:
    """Run a comprehensive multi-stage diagnostic on a single server."""
    name = server.name or "سرور بدون نام"
    server_type = getattr(server, "server_type", "xui") or "xui"
    base_url = (server.base_url or "").strip()

    if not base_url:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            is_ok=False,
            error_stage="CONFIG",
            error_title="آدرس سرور خالی است",
            error_detail="هیچ آدرس یا دامنه پایه‌ای برای این سرور تعریف نشده است.",
            recommendation="در بخش مدیریت سرور، آدرس URL معتبر وارد کنید.",
        )

    # 1. Parse URL
    try:
        parsed = urllib.parse.urlparse(base_url)
        host = parsed.hostname
        scheme = (parsed.scheme or "http").lower()
        default_port = 443 if scheme == "https" else 80
        port = parsed.port or default_port
        if not host:
            raise ValueError("Host is empty")
    except Exception as exc:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            is_ok=False,
            error_stage="CONFIG",
            error_title="فرمت آدرس URL نامعتبر است",
            error_detail=str(exc),
            recommendation="آدرس را به شکل کامل (مثلاً https://domain.com:2539) وارد کنید.",
        )

    # 2. DNS Resolution
    resolved_ip: str | None = None
    try:
        loop = asyncio.get_running_loop()
        addr_info = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
        if addr_info and addr_info[0] and addr_info[0][4]:
            resolved_ip = addr_info[0][4][0]
    except socket.gaierror as exc:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            is_ok=False,
            error_stage="DNS",
            error_title="خطای DNS (عدم تبدیل دامنه به IP)",
            error_detail=f"دامنه «{host}» توسط DNS سرور ترجمه نشد: {exc}",
            recommendation="تنظیمات DNS دامنه و رکوردهای A در کلودفلر یا پنل DNS را بررسی کنید.",
        )
    except Exception as exc:
        logger.warning("[DIAGNOSTIC] DNS check error for %s: %s", host, exc)

    # 3. TCP Connect & Latency Check
    start_time = time.monotonic()
    try:
        connect_target = resolved_ip or host
        conn_coro = asyncio.open_connection(connect_target, port)
        reader, writer = await asyncio.wait_for(conn_coro, timeout=min(timeout, 4.0))
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        tcp_latency_ms = round((time.monotonic() - start_time) * 1000, 1)
    except asyncio.TimeoutError:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=False,
            error_stage="TIMEOUT",
            error_title=f"تایم‌اوت ارتباط با پورت {port}",
            error_detail=f"سرور در بازه زمانی {timeout:.1f} ثانیه به پورت {port} پاسخ نداد.",
            recommendation=f"پورت {port} را در فایروال سرور (ufw/iptables) باز کنید یا از روشن بودن پنل مطمئن شوید.",
        )
    except ConnectionRefusedError as exc:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=False,
            error_stage="TCP",
            error_title=f"عدم پذیرش اتصال پورت {port} (Connection Refused)",
            error_detail=f"سرور پکت را رد کرد: {exc}",
            recommendation=f"سرویس پنل روی پورت {port} سرور اجرا نشده است یا پورت اشتباه تنظیم شده.",
        )
    except Exception as exc:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=False,
            error_stage="TCP",
            error_title="خطای ارتباط TCP",
            error_detail=str(exc),
            recommendation="شبکه و آی‌پی سرور را بررسی کنید.",
        )

    # 4. Strategy API & Auth Probe
    try:
        strategy = strategy_for_server(server)
        probe_coro = strategy.health_probe(server)
        await asyncio.wait_for(probe_coro, timeout=timeout)
        total_latency_ms = round((time.monotonic() - start_time) * 1000, 1)

        # Count active inbounds if available
        inbounds_count = len(server.inbounds) if server.inbounds else None

        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=True,
            latency_ms=total_latency_ms,
            inbounds_count=inbounds_count,
        )
    except asyncio.TimeoutError:
        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=False,
            latency_ms=tcp_latency_ms,
            error_stage="TIMEOUT",
            error_title="تایم‌اوت در پردازش درخواست API پنل",
            error_detail=f"پورت باز است اما پنل در {timeout:.1f} ثانیه پاسخ نداد.",
            recommendation="لود سرور یا منابع CPU/RAM سرور را بررسی کنید.",
        )
    except Exception as exc:
        err_msg = str(exc)
        err_lower = err_msg.lower()

        error_stage = "API"
        error_title = "خطای پاسخ پنل"
        recommendation = "لاگ‌های پنل و سرور را بررسی کنید."

        if "ssl" in err_lower or "certificate" in err_lower or isinstance(exc, ssl.SSLError):
            error_stage = "SSL"
            error_title = "خطای گواهی SSL / TLS"
            recommendation = "گواهی SSL سرور منقضی شده یا نامعتبر است. گزینه بررسی SSL را در تنظیمات خاموش کنید یا گواهی معتبر نصب کنید."
        elif "auth" in err_lower or "401" in err_lower or "403" in err_lower or "login" in err_lower or "unauthorized" in err_lower:
            error_stage = "AUTH"
            error_title = "خطای نام کاربری یا کلمه عبور (۴۰۱/۴۰۳)"
            recommendation = "نام کاربری یا رمز عبور پنل در ربات اشتباه است؛ لطفاً مشخصات ورود را ویرایش کنید."
        elif "404" in err_lower or "not found" in err_lower:
            error_stage = "API"
            error_title = "مسیر پنل پیدا نشد (کد ۴۰۴)"
            recommendation = "آدرس URL یا مسیر ساب‌پث پنل (مثلاً /panel یا پورت) اشتباه وارد شده است."
        elif "500" in err_lower or "502" in err_lower or "503" in err_lower or "bad gateway" in err_lower:
            error_stage = "API"
            error_title = "خطای داخلی پنل (کد ۵۰۰/۵۰۲)"
            recommendation = "هسته پنل یا وب‌سرور داخلی آن کرش کرده است؛ پنل را روی سرور ری‌استارت کنید."

        return ServerDiagnosticResult(
            server_id=server.id,
            server_name=name,
            server_type=server_type,
            base_url=base_url,
            port=port,
            resolved_ip=resolved_ip,
            is_ok=False,
            latency_ms=tcp_latency_ms,
            error_stage=error_stage,
            error_title=error_title,
            error_detail=err_msg[:250],
            recommendation=recommendation,
        )


async def diagnose_all_servers(
    session: AsyncSession,
    *,
    timeout: float = 6.0,
) -> list[ServerDiagnosticResult]:
    """Diagnose all active servers in parallel."""
    result = await session.execute(
        select(XUIServerRecord)
        .options(
            selectinload(XUIServerRecord.credentials),
            selectinload(XUIServerRecord.inbounds),
        )
        .where(XUIServerRecord.health_status != "deleted")
        .order_by(XUIServerRecord.created_at.asc())
    )
    servers = list(result.scalars().all())
    if not servers:
        return []

    tasks = [diagnose_server(s, timeout=timeout) for s in servers]
    return await asyncio.gather(*tasks)


def format_single_server_diagnostic(res: ServerDiagnosticResult) -> str:
    """Format detailed diagnostic for one server."""
    status_icon = "🟢" if res.is_ok else "🔴"
    status_text = "متصل و آماده ✅" if res.is_ok else "قطع یا دارای خطا ❌"

    lines = [
        f"{status_icon} <b>گزارش تست اتصال سرور: «{res.server_name}»</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏷 <b>نوع پنل</b>: <code>{res.server_type.upper()}</code>",
        f"🌐 <b>آدرس</b>: <code>{res.base_url}</code>",
    ]
    if res.resolved_ip:
        lines.append(f"📍 <b>آی‌پی سرور</b>: <code>{res.resolved_ip}</code> (پورت: <code>{res.port}</code>)")
    if res.latency_ms is not None:
        lines.append(f"⚡ <b>زمان پاسخ (Ping)</b>: <code>{res.latency_ms} ms</code>")
    if res.inbounds_count is not None:
        lines.append(f"📦 <b>تعداد اینباندها</b>: <code>{res.inbounds_count}</code>")
    lines.append(f"📊 <b>وضعیت نهایی</b>: <b>{status_text}</b>")

    if not res.is_ok:
        lines.append("")
        lines.append("⚠️ <b>علت عدم اتصال:</b>")
        if res.error_stage:
            lines.append(f"  • <b>مرحله خطا</b>: <code>{res.error_stage}</code>")
        if res.error_title:
            lines.append(f"  • <b>عنوان خطا</b>: <b>{res.error_title}</b>")
        if res.error_detail:
            lines.append(f"  • <b>جزئیات فنی</b>: <code>{res.error_detail}</code>")
        if res.recommendation:
            lines.append("")
            lines.append(f"💡 <b>راهنمای رفع مشکل:</b>\n{res.recommendation}")

    return "\n".join(lines)


def format_all_servers_diagnostic(results: list[ServerDiagnosticResult]) -> str:
    """Format summary diagnostic report for all servers."""
    if not results:
        return "📭 هیچ سروری برای بررسی یافت نشد."

    healthy_count = sum(1 for r in results if r.is_ok)
    total_count = len(results)

    header = (
        f"🔍 <b>گزارش جامع تست اتصال سرورها</b>\n"
        f"📊 سرورهای متصل: <b>{healthy_count} از {total_count}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
    )

    blocks: list[str] = []
    for res in results:
        if res.is_ok:
            latency = f"<code>{res.latency_ms} ms</code>" if res.latency_ms is not None else "-"
            blocks.append(
                f"🟢 <b>{res.server_name}</b> ({res.server_type.upper()})\n"
                f"   ├ 🌐 آدرس: <code>{res.base_url}</code>\n"
                f"   ├ ⚡ پینگ: {latency}\n"
                f"   └ ✅ وضعیت: <b>متصل و پاسخگو</b>"
            )
        else:
            blocks.append(
                f"🔴 <b>{res.server_name}</b> ({res.server_type.upper()})\n"
                f"   ├ 🌐 آدرس: <code>{res.base_url}</code>\n"
                f"   ├ ❌ مرحله: <b>{res.error_stage or 'خطا'}</b> — {res.error_title or 'عدم پاسخ'}\n"
                f"   ├ ⚠️ جزئیات: <code>{res.error_detail or 'نامشخص'}</code>\n"
                f"   └ 💡 راهنما: {res.recommendation or 'سرور را بررسی کنید.'}"
            )

    return header + "\n\n".join(blocks)
