"""
Admin-side flow: import users + orders from the previous bot's MySQL dump
by uploading the .sql file directly to this bot as a Telegram document.

Operator UX
-----------
1. /admin → "⚙️ پنل مدیریت" → settings → "📥 ایمپورت دیتابیس ربات قبلی"
2. Bot asks for the `.sql` dump as a document.
3. Operator drops the file into the chat.
4. Bot edits its status message live: "⏳ در حال دانلود فایل…" →
   "🔍 در حال پردازش…" → final summary with counts.

Implementation notes
--------------------
* The actual parsing / inserting logic lives in `scripts/import_legacy.py`
  (Phase-2 commit 104b75e). We call its `run()` coroutine directly —
  no subprocess, no separate session, no extra moving parts. The
  script's runner already owns its own AsyncSessionFactory so the
  bot's own session (held by middleware) doesn't get tangled in the
  long-running import transaction.

* Two safety gates before we even start parsing:
    1. Admin-only — we mount AdminOnlyMiddleware on the router (same
       pattern every other admin module uses).
    2. File extension must be `.sql` and size must be < `_MAX_DUMP_BYTES`
       (default 25 MB) so an operator who drops a 4 GB log file by
       accident gets a friendly error instead of a chat freeze.

* Telegram bot API caps `bot.download_file()` at 20 MB by default; if
  the operator runs against a local Bot API server they can lift this
  by setting the limit higher in their server config. We surface the
  Telegram error verbatim if it happens.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from apps.bot.middlewares.admin import AdminOnlyMiddleware
from apps.bot.states.admin import LegacyImportStates
from apps.bot.utils.messaging import safe_edit_or_send


logger = logging.getLogger(__name__)

router = Router(name="admin-legacy-import")
router.message.middleware(AdminOnlyMiddleware())
router.callback_query.middleware(AdminOnlyMiddleware())


# 25 MB. The earlier dump the operator shared was ~7 MB so this leaves
# plenty of headroom for future imports without giving them so much
# slack that a misplaced backup of /var/log slips through.
_MAX_DUMP_BYTES = 25 * 1024 * 1024


# ── Entry: callback opens the upload screen ─────────────────────────────


@router.callback_query(F.data == "admin:settings:legacy_import")
async def legacy_import_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    await state.set_state(LegacyImportStates.waiting_for_dump)

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ انصراف", callback_data="admin:settings:legacy_import:cancel")
    builder.adjust(1)

    text = (
        "📥 <b>ایمپورت دیتابیس ربات قبلی</b>\n"
        "━━━━━━━━━━━━━━\n"
        "فایل dump با پسوند <code>.sql</code> (خروجی phpMyAdmin) رو "
        "همینجا به‌صورت <b>سند (Document)</b> آپلود کن.\n\n"
        "بات کار رو انجام می‌ده:\n"
        "  • کاربرها بر اساس <b>telegram_id</b> match می‌شن\n"
        "  • کیف پول تومن قدیمی با نرخ فعلی به دلار تبدیل می‌شه\n"
        "  • کانفیگ‌های قدیمی با اسم اصلی حفظ می‌شن (legacy_remark)\n"
        "  • کاربر‌ها / کانفیگ‌های موجود overwrite نمی‌شن — فقط جدیدها اضافه می‌شن\n\n"
        f"⚠️ حداکثر حجم فایل: <b>{_MAX_DUMP_BYTES // (1024*1024)} MB</b>\n"
        "این عملیات قابل بازگشت نیست؛ قبلش backup گرفته باشی."
    )
    await safe_edit_or_send(callback, text, reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data == "admin:settings:legacy_import:cancel")
async def legacy_import_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("لغو شد.")
    await state.clear()
    await safe_edit_or_send(callback, "❌ ایمپورت لغو شد.")


# ── Receive the document + run the import ───────────────────────────────


@router.message(LegacyImportStates.waiting_for_dump, F.document)
async def legacy_import_receive(message: Message, state: FSMContext, bot: Bot) -> None:
    """Download + import the operator-uploaded SQL dump."""
    doc: Document = message.document  # type: ignore[assignment]
    if doc is None:
        return

    fname = (doc.file_name or "").strip()
    if not fname.lower().endswith(".sql"):
        await message.reply(
            "❌ فقط فایل با پسوند <code>.sql</code> قابل قبول است.\n"
            "اگر فایل فشرده‌ست (مثل <code>.sql.gz</code>)، اول روی کامپیوتر "
            "خودت اون رو extract کن و نسخه‌ی <code>.sql</code> رو بفرست.",
            parse_mode="HTML",
        )
        return

    size = doc.file_size or 0
    if size > _MAX_DUMP_BYTES:
        await message.reply(
            f"❌ حجم فایل ({size // (1024*1024)} MB) بیش از حد مجاز "
            f"({_MAX_DUMP_BYTES // (1024*1024)} MB) است.\n"
            "از روش CLI استفاده کن — راهنما در commit message Phase 2.",
            parse_mode="HTML",
        )
        return

    progress = await message.reply(
        f"⏳ در حال دانلود فایل ({size // 1024} KB)…",
        parse_mode="HTML",
    )

    # Download to a temp file. Don't leak it if the import fails mid-way.
    fd, dump_path_str = tempfile.mkstemp(prefix="legacy_dump_", suffix=".sql")
    os.close(fd)
    dump_path = Path(dump_path_str)

    try:
        # aiogram-3 download_file writes to a path or a BytesIO.
        await bot.download(doc, destination=dump_path_str)

        await _safe_edit(progress, "🔍 در حال خواندن و پردازش فایل…\n<i>(می‌تونه چند ثانیه طول بکشه)</i>")

        # Run the import. We don't pass --dry-run; the operator already
        # confirmed intent by reaching this point. The `run()` coroutine
        # uses its OWN session via AsyncSessionFactory so it never
        # tangles with the bot's per-message session.
        from scripts.import_legacy import run as run_import
        stats = await run_import(dump_path, dry_run=False, limit=0)

        # Final summary. Keep it dense — Telegram caption-ish.
        summary = (
            "✅ <b>ایمپورت کامل شد</b>\n"
            "━━━━━━━━━━━━━━\n"
            "<b>کاربران</b>\n"
            f"  دیده‌شده:  <b>{stats.users_seen}</b>\n"
            f"  افزوده‌شده: <b>{stats.users_inserted}</b>\n"
            f"  از قبل بوده: <b>{stats.users_skipped_existing}</b>\n"
            f"  ناموفق: <b>{stats.users_failed}</b>\n"
            f"  کیف پول تومن→دلار: <b>{stats.wallet_credited}</b> ردیف\n\n"
            "<b>سرویس‌ها (orders)</b>\n"
            f"  دیده‌شده: <b>{stats.orders_seen}</b>\n"
            f"  افزوده‌شده: <b>{stats.orders_inserted}</b>\n"
            f"  اصلاح‌شده: <b>{getattr(stats, 'orders_updated', 0)}</b>\n"
            f"  تکراری: <b>{stats.orders_skipped_duplicate}</b>\n"
            f"  ناموفق: <b>{stats.orders_failed}</b>\n"
            "━━━━━━━━━━━━━━\n"
            "حالا کاربران قدیمی می‌تونن <b>/start</b> بزنن و کانفیگ‌هاشون رو "
            "ببینن. کانفیگ‌های imported با علامت 🗂 توی لیست مشخص می‌شن و "
            "قابلیت انتقال به اینباند جدید رو دارن (با حفظ نام)."
        )
        await _safe_edit(progress, summary)

    except FileNotFoundError as exc:
        logger.error("Legacy import — file vanished: %s", exc)
        await _safe_edit(progress, "❌ فایل قابل دسترس نیست. لطفاً دوباره تلاش کن.")
    except Exception as exc:
        logger.error("Legacy import failed: %s", exc, exc_info=True)
        msg = str(exc)
        await _safe_edit(
            progress,
            f"❌ <b>خطا در ایمپورت</b>\n"
            f"<code>{_esc(msg[:500])}</code>\n\n"
            "هیچ تغییری در دیتابیس اعمال نشد (مگر آنکه خطای پس از commit "
            "اولیه رخ داده باشد).",
        )
    finally:
        await state.clear()
        try:
            dump_path.unlink(missing_ok=True)
        except Exception:
            pass


@router.message(LegacyImportStates.waiting_for_dump)
async def legacy_import_wrong_type(message: Message) -> None:
    """Operator typed text instead of attaching a file."""
    await message.reply(
        "⚠️ فایل <code>.sql</code> رو به‌صورت <b>سند (Document)</b> آپلود کن، "
        "نه به‌صورت متن. اگر می‌خوای از فلو خارج بشی روی «انصراف» بزن یا "
        "<code>/cancel</code> تایپ کن.",
        parse_mode="HTML",
    )


# ── helpers ─────────────────────────────────────────────────────────────


async def _safe_edit(target_message: Message, text: str) -> None:
    """Edit the progress message, fall back to a fresh send if the
    original was deleted in the meantime."""
    try:
        await target_message.edit_text(text, parse_mode="HTML")
    except Exception:
        try:
            await target_message.answer(text, parse_mode="HTML")
        except Exception:
            pass


def _esc(s: str) -> str:
    """Minimal HTML escape — only the characters that break Telegram parsing."""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))
