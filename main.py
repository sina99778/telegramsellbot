import asyncio
import math
from html import escape
from urllib.parse import unquote
from telegram.error import BadRequest
import requests
import uuid
import re
import json
import time
import traceback
import pytz
from datetime import datetime, timedelta, timezone
import qrcode
from io import BytesIO
import telegram
from dateutil.relativedelta import relativedelta
import logging
import psycopg2
import os
from dotenv import load_dotenv
from telegram.error import TimedOut, NetworkError
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, InputFile
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from random import randint
from psycopg2.pool import SimpleConnectionPool

load_dotenv()

REQ_TIMEOUT = 10  # seconds

TOKEN = os.getenv("BOT_TOKEN")
channel_username = os.getenv("CHANNEL_USERNAME")
NOWPAYMENTS_API_KEY = os.getenv("NOWPAYMENTS_API_KEY")
NOWPAYMENTS_BASE_URL = os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io/v1")
NOWPAYMENTS_PRICE_CURRENCY = os.getenv("NOWPAYMENTS_PRICE_CURRENCY", "usd")
NOWPAYMENTS_PAY_CURRENCY = os.getenv("NOWPAYMENTS_PAY_CURRENCY", "usdttrc20")
USD_TO_TOMAN_RATE = float(os.getenv("USD_TO_TOMAN_RATE", "100000"))

FIELD_MAP = {
    "url": "base_url",
    "name": "name",
    "username": "username",
    "password": "password",
    "loc": "location",
    "active": "is_active",
    "maxgb": "max_traffic_gb",
    "users": "max_users"
}

DB_KW = dict(
    host=os.getenv("DB_HOST"),
    dbname=os.getenv("DB_NAME"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASS"),
)

pool = SimpleConnectionPool(1, 20, **DB_KW)

PAGE_SIZE = 5
SYNC_CACHE_KEY = "sync_check_cache"  # context.user_data[...]
SYNC_PAGE_KEY = "sync_check_page"
TIMED_KEY_PREFIX = "_expires_at_"
STATE_KEY = "state"
STATE_TIMEOUT_SECONDS = 600

STATE_IDLE = "idle"
STATE_BACK_ADMIN_PANEL = "back_admin_panel"
STATE_BACK_SETUP_SERVERS = "back_setup_servers"
STATE_BACK_PROFILE_PANEL = "back_profile_panel"
STATE_BACK_BUY_SERVER_PANEL = "back_buy_server_panel"
STATE_AWAITING_ADMIN_USER_MESSAGE = "awaiting_admin_user_message"
STATE_AWAITING_ADMIN_REPLY = "awaiting_admin_reply"
STATE_AWAITING_EDIT_SERVER_VALUE = "awaiting_edit_server_value"
STATE_AWAITING_EDIT_PLAN_VALUE = "awaiting_edit_plan_value"
STATE_AWAITING_SUPPORT_MESSAGE = "awaiting_support_message"
STATE_AWAITING_WALLET_AMOUNT = "awaiting_wallet_amount"
STATE_AWAITING_WALLET_CONFIRM = "awaiting_wallet_confirm"
STATE_AWAITING_WALLET_RECEIPT = "awaiting_wallet_receipt"
STATE_AWAITING_NOWPAYMENT = "awaiting_nowpayment"
STATE_AWAITING_PROMO_PERCENT = "awaiting_promo_percent"
STATE_AWAITING_PROMO_END = "awaiting_promo_end"

ALLOWED_SERVER_EDIT_FIELDS = {"base_url", "name", "username", "password", "location", "is_active", "max_traffic_gb", "max_users"}
ALLOWED_PLAN_EDIT_FIELDS = {"price", "inbound_id", "traffic_gb", "duration_days"}

CALLBACK_PATTERNS = [
    (re.compile(r"^(?P<name>wallet)_(?P<action>appr|rej)_(?P<id>\d+)$"), lambda m: {"name": "wallet_admin", "action": m["action"], "id": int(m["id"])}),
    (re.compile(r"^(?P<name>reward)_(?P<action>approve|reject)_(?P<id>\d+)$"), lambda m: {"name": "reward", "action": m["action"], "id": int(m["id"])}),
    (re.compile(r"^(?P<name>user_view_category|user_buy_plan|confirm_buy_plan|user_purchased_plan|renewal_purchased_plan|confirm_renewal|delete_purchased_plan|change_link_purchased_plan|change_name_purchased_plan|dis_able_purchased_plan|selected_user|users_page|reply_to|user_cart_vis|user_change_balance|user_delete|confirm_user_delete|user_in_active|user_discount_percentage|admin_view_category|admin_view_plan|edit_server|delete_server)_(?P<id>\d+)$"),
     lambda m: {"name": m["name"], "id": int(m["id"])}),
    (re.compile(r"^(?P<name>user_plans|user_transactions)_(?P<user_id>\d+)_(?P<page>\d+)$"),
     lambda m: {"name": m["name"], "user_id": int(m["user_id"]), "page": int(m["page"])}),
    (re.compile(r"^(?P<name>all_message|unanswered_message)_(?P<page>\d+)$"),
     lambda m: {"name": m["name"], "page": int(m["page"])}),
    (re.compile(r"^(?P<name>confirm_user)_(?P<action>in_active|active)_(?P<id>\d+)$"),
     lambda m: {"name": "confirm_user_status", "action": m["action"], "id": int(m["id"])}),
    (re.compile(r"^(?P<name>search_user)_(?P<action>username|userid)$"),
     lambda m: {"name": m["name"], "action": m["action"]}),
    (re.compile(r"^(?P<name>callback_edit_server)_(?P<field>\w+)_(?P<index>\d+)$"),
     lambda m: {"name": m["name"], "field": m["field"], "index": int(m["index"])}),
    (re.compile(r"^(?P<name>confirm_edit_plan)_(?P<field>price|inbound_id|traffic_gb|duration_days)_(?P<id>\d+)$"),
     lambda m: {"name": m["name"], "field": m["field"], "id": int(m["id"])}),
    (re.compile(r"^(?P<name>confirm_edit)_(?P<field>\w+)_(?P<index>\d+)$"),
     lambda m: {"name": m["name"], "field": m["field"], "index": int(m["index"])}),
    (re.compile(r"^(?P<name>sync_page|sync_recheck|sync_ignore|sync_item):(?P<value>[^:]+)$"),
     lambda m: {"name": m["name"], "value": m["value"]}),
]


def parse_callback(data: str) -> dict:
    raw = (data or "").strip()
    for pattern, builder in CALLBACK_PATTERNS:
        match = pattern.match(raw)
        if match:
            parsed = builder(match)
            parsed["raw"] = raw
            return parsed
    return {"name": raw, "raw": raw}


def set_timed_value(context: ContextTypes.DEFAULT_TYPE, key: str, value, ttl_seconds: int = STATE_TIMEOUT_SECONDS):
    context.user_data[key] = value
    context.user_data[f"{TIMED_KEY_PREFIX}{key}"] = time.time() + ttl_seconds


def get_timed_value(context: ContextTypes.DEFAULT_TYPE, key: str, default=None):
    if key not in context.user_data:
        return default
    expires_at = context.user_data.get(f"{TIMED_KEY_PREFIX}{key}")
    if expires_at is not None and time.time() > expires_at:
        context.user_data.pop(key, None)
        context.user_data.pop(f"{TIMED_KEY_PREFIX}{key}", None)
        return default
    return context.user_data.get(key, default)


def clear_timed_value(context: ContextTypes.DEFAULT_TYPE, key: str):
    context.user_data.pop(key, None)
    context.user_data.pop(f"{TIMED_KEY_PREFIX}{key}", None)


def set_user_state(context: ContextTypes.DEFAULT_TYPE, state: str):
    set_timed_value(context, STATE_KEY, state)


def get_user_state(context: ContextTypes.DEFAULT_TYPE):
    return get_timed_value(context, STATE_KEY)


def clear_user_state(context: ContextTypes.DEFAULT_TYPE):
    clear_timed_value(context, STATE_KEY)


def sanitize_text_input(text: str, *, max_len: int, field_name: str = "input", allow_empty: bool = False) -> str:
    value = (text or "").strip()
    if not value and not allow_empty:
        raise ValueError(f"{field_name} is required")
    if len(value) > max_len:
        raise ValueError(f"{field_name} is too long")
    return value


def sanitize_server_field_value(field: str, raw_value: str):
    if field not in ALLOWED_SERVER_EDIT_FIELDS:
        raise ValueError("invalid server field")
    value = (raw_value or "").strip()
    if field in {"base_url", "name", "username", "password", "location"}:
        max_len = 255 if field == "base_url" else 128
        return sanitize_text_input(value, max_len=max_len, field_name=field)
    if field == "is_active":
        lowered = value.lower()
        if lowered in {"true", "1", "yes", "بله", "فعال"}:
            return True
        if lowered in {"false", "0", "no", "خیر", "نه", "غیرفعال"}:
            return False
        raise ValueError("invalid boolean value")
    if field == "max_traffic_gb":
        return float(value)
    if field == "max_users":
        parsed = int(value)
        if parsed < 0:
            raise ValueError("max_users must be >= 0")
        return parsed
    raise ValueError("invalid server field")


def sanitize_plan_field_value(field: str, raw_value: str):
    if field not in ALLOWED_PLAN_EDIT_FIELDS:
        raise ValueError("invalid plan field")
    value = (raw_value or "").strip()
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{field} must be >= 0")
    return parsed


def get_promo_settings():
    row = q_one("""
        SELECT enabled, percent, end_at
        FROM promo_settings
        WHERE enabled = TRUE
          AND (end_at IS NULL OR end_at > NOW())
        ORDER BY id DESC
        LIMIT 1
    """)
    if not row:
        return {"enabled": False, "percent": 0.0, "end_at": None}

    enabled, percent, end_at = row
    if end_at is not None and end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    elif end_at is not None:
        end_at = end_at.astimezone(timezone.utc)
    return {"enabled": bool(enabled), "percent": float(percent or 0), "end_at": end_at}


def is_promo_active_now() -> tuple[bool, float]:
    s = get_promo_settings()
    if not s["enabled"]:
        return False, 0.0
    if s["percent"] <= 0:
        return False, 0.0
    if s["end_at"] is not None:
        if datetime.now(timezone.utc) >= s["end_at"]:
            return False, 0.0
    return True, s["percent"]


def apply_traffic_promo(base_gb: float, percent: float) -> int:
    bonus = (base_gb * percent) / 100.0
    effective = base_gb + bonus
    return int(math.ceil(effective))


def set_promo(enabled: bool, percent: float, end_at, updated_by: int):
    q_exec("""
        INSERT INTO promo_settings (id, enabled, percent, end_at, updated_by, updated_at)
        VALUES (1, %s, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE
        SET enabled=EXCLUDED.enabled,
            percent=EXCLUDED.percent,
            end_at=EXCLUDED.end_at,
            updated_by=EXCLUDED.updated_by,
            updated_at=NOW()
    """, (enabled, percent, end_at, updated_by))


def disable_promo(updated_by: int):
    q_exec("""
        INSERT INTO promo_settings (id, enabled, percent, end_at, updated_by, updated_at)
        VALUES (1, FALSE, 0, NULL, %s, NOW())
        ON CONFLICT (id) DO UPDATE
        SET enabled=FALSE,
            percent=0,
            end_at=NULL,
            updated_by=EXCLUDED.updated_by,
            updated_at=NOW()
    """, (updated_by,))


def q_one_tx(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


def q_all_tx(cur, sql, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def q_all(sql, params=()):
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    finally:
        pool.putconn(conn)


def q_one(sql, params=()):
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchone()
    finally:
        pool.putconn(conn)


def q_exec(sql, params=()):
    """برای INSERT/UPDATE/DELETE – خروجی: rowcount"""
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.rowcount
    finally:
        pool.putconn(conn)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error_text = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    log_path = os.path.join(os.path.dirname(__file__), "bot_errors.log")
    logger.exception("Unhandled exception")
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"\n{'=' * 80}\n{datetime.now().isoformat()}\n{error_text}")
    except Exception:
        logger.exception("Failed to persist traceback to file")

    # اختیاری: گزارش به ادمین
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"⚠️ Bot Error:\n<code>{escape(str(context.error))}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID"))

CARD_NUMBER = "6219-8618-1086-0469"

IR_MOBILE_RE = re.compile(r'^(?:\+?98|0098|0)9\d{9}$')

PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

REWARD_LADDER = [
    {"level": 1, "invites": 5, "title": "۳ روز VIP (۵GB)"},
    {"level": 2, "invites": 10, "title": "۵ روز VIP (۱۰GB)"},
    {"level": 3, "invites": 20, "title": "۷ روز VIP (۲۰GB)"},
    {"level": 4, "invites": 35, "title": "۱۵ روز VIP (۵۰GB)"},
    {"level": 5, "invites": 50, "title": "۳۰ روز VIP (۱۰۰GB)"},
]


async def notify_admin_plan_event(context, *, event: str, plan_id: int, telegram_id: int,
                                  plan_name: str | None, expiry_date=None,
                                  used_gb=None, max_gb=None):
    try:
        name = plan_name or "بدون نام"
        exp_txt = expiry_date.strftime("%Y-%m-%d %H:%M") if expiry_date else "—"

        if event == "traffic_exceeded":
            text = (
                "🚫 <b>Deactivated: Traffic exceeded</b>\n"
                f"🆔 PlanID: <code>{plan_id}</code>\n"
                f"👤 TG: <code>{telegram_id}</code>\n"
                f"🏷 Name: <code>{escape(name)}</code>\n"
                f"📊 Usage: <b>{used_gb}/{max_gb}</b> GB\n"
                f"🗓 Expiry: <b>{exp_txt}</b>\n"
            )
        elif event == "expired":
            text = (
                "⛔️ <b>Deactivated: Expired</b>\n"
                f"🆔 PlanID: <code>{plan_id}</code>\n"
                f"👤 TG: <code>{telegram_id}</code>\n"
                f"🏷 Name: <code>{escape(name)}</code>\n"
                f"🗓 Expiry: <b>{exp_txt}</b>\n"
            )
        else:
            return

        await context.bot.send_message(chat_id=ADMIN_ID, text=text, parse_mode="HTML")
    except Exception:
        logger.exception("notify_admin_plan_event failed")


async def check_traffic_usage(context):
    job = getattr(context, "job", None)
    job_name = getattr(job, "name", "check_traffic_usage")
    processed = 0
    logger.info("%s started", job_name)
    try:
        plans = q_all("""
            SELECT
                pp.id,
                pp.user_id,
                pp.name,
                pp.expiry_date,
                pp.config_data,
                pp.max_traffic_gb,
                u.user_id AS telegram_id
            FROM purchased_plans pp
            JOIN users u ON pp.user_id = u.id
            WHERE pp.is_active = TRUE
            ORDER BY pp.id ASC
            LIMIT 100
        """)
        logger.info("%s loaded %s active plans", job_name, len(plans))

        for plan_id, db_user_id, plan_name, expiry_date, config_data_json, max_gb, telegram_id in plans:
            try:
                processed += 1
                config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
                uuid = config_data.get("uuid")
                address = config_data.get("address")
                if not uuid or not address:
                    raise ValueError("uuid/address missing")

                srv = q_one("SELECT base_url, username, password FROM servers WHERE address = %s", (address,))
                if not srv:
                    logger.warning("%s server not found for address=%s plan_id=%s", job_name, address, plan_id)
                    continue

                base_url, username, password = srv
                traffic_data = await fetch_panel_client_traffic(
                    base_url=base_url,
                    username=username,
                    password=password,
                    client_uuid=uuid,
                )
                obj = traffic_data.get("obj", [])[0]
                used_gb = round((obj.get("up", 0) + obj.get("down", 0)) / (1024 ** 3), 2)

                max_gb = float(max_gb or 0)
                percent = (used_gb / max_gb) * 100 if max_gb > 0 else 0
                exp_txt = expiry_date.strftime("%Y-%m-%d") if expiry_date else "—"

                if max_gb > 0 and used_gb >= max_gb:
                    q_exec("""
                        UPDATE purchased_plans
                        SET
                            is_active = FALSE,
                            deactivated_reason = 'traffic_exceeded',
                            deactivated_at = NOW(),
                            deactivated_by = 'job_traffic'
                        WHERE id = %s
                    """, (plan_id,))

                    await context.bot.send_message(
                        telegram_id,
                        "📉 <b>حجم کانفیگ شما تمام شد</b>\n\n"
                        f"کانفیگ «<b>{plan_name or 'بدون نام'}</b>» به دلیل اتمام حجم، فعلاً غیرفعال شد.\n\n"
                        f"📊 مصرف: <b>{used_gb}/{max_gb}</b> گیگ\n"
                        f"🗓 انقضا: <b>{exp_txt}</b>\n\n"
                        "برای ادامه استفاده، لطفاً آن را تمدید کنید یا پلن جدید بخرید.",
                        parse_mode="HTML"
                    )
                    await notify_admin_plan_event(
                        context,
                        event="traffic_exceeded",
                        plan_id=plan_id,
                        telegram_id=telegram_id,
                        plan_name=plan_name,
                        expiry_date=expiry_date,
                        used_gb=used_gb,
                        max_gb=max_gb,
                    )
                    continue

                if 80 <= percent < 100:
                    ins = q_one("""
                        INSERT INTO warnings_sent (plan_id, type, sent_at)
                        VALUES (%s, 'traffic_80', NOW())
                        ON CONFLICT (plan_id, type) DO NOTHING
                        RETURNING id
                    """, (plan_id,))

                    if ins:
                        remaining_gb = max(0, round(max_gb - used_gb, 2))
                        await context.bot.send_message(
                            telegram_id,
                            "⚠️ <b>هشدار مصرف حجم</b>\n\n"
                            f"بیش از <b>۸۰٪</b> حجم کانفیگ «<b>{plan_name or 'بدون نام'}</b>» مصرف شده است.\n\n"
                            f"📦 باقی‌مانده: <b>{remaining_gb}</b> گیگ\n"
                            f"📊 مصرف: <b>{used_gb}/{max_gb}</b> گیگ\n"
                            f"🗓 انقضا: <b>{exp_txt}</b>\n\n"
                            "برای جلوگیری از قطع اتصال، پیشنهاد می‌کنیم از الان برای تمدید اقدام کنید.",
                            parse_mode="HTML"
                        )
            except Exception:
                logger.exception("%s plan %s failed", job_name, plan_id)

        logger.info("%s finished, processed=%s", job_name, processed)
    except Exception:
        logger.exception("%s failed", job_name)


async def check_expiry_dates(context):
    job = getattr(context, "job", None)
    job_name = getattr(job, "name", "check_expiry_dates")
    processed = 0
    logger.info("%s started", job_name)
    try:
        plans = q_all("""
            SELECT pp.id, pp.user_id, pp.name, pp.expiry_date, u.user_id AS telegram_id
            FROM purchased_plans pp
            JOIN users u ON pp.user_id = u.id
            WHERE pp.is_active = TRUE AND pp.expiry_date IS NOT NULL
            ORDER BY pp.expiry_date ASC, pp.id ASC
            LIMIT 100
        """)
        logger.info("%s loaded %s active plans", job_name, len(plans))

        now = datetime.now()
        for plan_id, db_user_id, plan_name, expiry_date, telegram_id in plans:
            try:
                processed += 1
                if not expiry_date:
                    continue

                delta = (expiry_date - now).total_seconds()

                if delta <= 0:
                    q_exec("""
                        UPDATE purchased_plans
                        SET
                            is_active = FALSE,
                            deactivated_reason = 'expired',
                            deactivated_at = NOW(),
                            deactivated_by = 'job_expiry'
                        WHERE id = %s
                    """, (plan_id,))

                    q_exec("""
                        INSERT INTO warnings_sent (plan_id, type, sent_at)
                        VALUES (%s, 'expired', NOW())
                        ON CONFLICT (plan_id, type) DO NOTHING
                    """, (plan_id,))

                    await context.bot.send_message(
                        telegram_id,
                        "❌ <b>کانفیگ شما منقضی شد</b>\n\n"
                        f"کانفیگ «<b>{plan_name or 'بدون نام'}</b>» منقضی شده و دیگر فعال نیست.\n\n"
                        "برای ادامه استفاده، لطفاً آن را تمدید کنید یا پلن جدید بخرید.",
                        parse_mode="HTML"
                    )
                    await notify_admin_plan_event(
                        context,
                        event="expired",
                        plan_id=plan_id,
                        telegram_id=telegram_id,
                        plan_name=plan_name,
                        expiry_date=expiry_date,
                    )
                    continue

                if delta <= 24 * 3600:
                    ins = q_one("""
                        INSERT INTO warnings_sent (plan_id, type, sent_at)
                        VALUES (%s, 'expire_24h', NOW())
                        ON CONFLICT (plan_id, type) DO NOTHING
                        RETURNING id
                    """, (plan_id,))

                    if ins:
                        await context.bot.send_message(
                            telegram_id,
                            "⏳ <b>نزدیک انقضا</b>\n\n"
                            f"کمتر از <b>۲۴ ساعت</b> تا انقضای کانفیگ «<b>{plan_name or 'بدون نام'}</b>» باقی مانده است.\n\n"
                            f"📅 تاریخ انقضا: <b>{expiry_date:%Y-%m-%d %H:%M}</b>\n\n"
                            "برای جلوگیری از قطع اتصال، پیشنهاد می‌کنیم همین الان برای تمدید اقدام کنید.",
                            parse_mode="HTML"
                        )
                    continue

                if delta <= 3 * 24 * 3600:
                    ins = q_one("""
                        INSERT INTO warnings_sent (plan_id, type, sent_at)
                        VALUES (%s, 'expire_3d', NOW())
                        ON CONFLICT (plan_id, type) DO NOTHING
                        RETURNING id
                    """, (plan_id,))

                    if ins:
                        days_left = max(1, int(delta // (24 * 3600)))
                        await context.bot.send_message(
                            telegram_id,
                            "⏰ <b>یادآوری انقضا</b>\n\n"
                            f"حدود <b>{days_left} روز</b> تا انقضای کانفیگ «<b>{plan_name or 'بدون نام'}</b>» باقی مانده است.\n\n"
                            f"📅 تاریخ انقضا: <b>{expiry_date:%Y-%m-%d %H:%M}</b>\n\n"
                            "اگر می‌خواهید بدون قطعی ادامه دهید، پیشنهاد می‌کنیم از الان برای تمدید اقدام کنید.",
                            parse_mode="HTML"
                        )
            except Exception:
                logger.exception("%s plan %s failed", job_name, plan_id)

        logger.info("%s finished, processed=%s", job_name, processed)
    except Exception:
        logger.exception("%s failed", job_name)


def normalize_iran_phone(raw: str) -> str | None:
    """Return phone in '+989XXXXXXXXX' or None if invalid."""
    if not raw:
        return None
    s = raw.translate(PERSIAN_DIGITS)  # convert Persian/Arabic digits → Latin
    s = re.sub(r'[\s\-\(\)]', '', s)  # strip spaces, dashes, parens

    if not IR_MOBILE_RE.match(s):
        return None

    # Canonicalize to +989XXXXXXXXX
    if s.startswith('0098'):
        s = '+' + s[2:]  # 0098... → +98...
    elif s.startswith('98'):
        s = '+' + s  # 98... → +98...
    elif s.startswith('0'):
        s = '+98' + s[1:]  # 09... → +989...
    elif s.startswith('+98'):
        pass  # already OK
    else:
        return None

    return s


async def send_qr_code(update, context, url: str):
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white")

    # Convert to BytesIO
    buffer = BytesIO()
    img.save(buffer, format='PNG')
    buffer.seek(0)

    # Send QR code to user
    await update.effective_message.reply_photo(photo=InputFile(buffer, filename="qr.png"),
                                               caption="📱 این کد QR مربوط به کانفیگ شماست. برای افزودن آن به برنامه، کافیست آن را اسکن کنید."
                                               )


async def send_config_with_qr(update, context, *, url: str, max_gb: int, duration_days: int, expiry_date: datetime,
                              prefix_text: str = "✅ خرید شما با موفقیت انجام شد!", subscription_url: str | None = None):
    await send_qr_code(update, context, url)
    subscription_block = ""
    if subscription_url:
        subscription_block = f"✨ <b>لینک اشتراک:</b>\n<code>{subscription_url}</code>\n"
    await update.effective_message.reply_text(
        f"{prefix_text}\n\n"
        f"📦 <b>حجم:</b> {max_gb}GB\n"
        f"⏳ <b>مدت اعتبار:</b> {duration_days} روز\n"
        f"🗓 <b>تاریخ انقضا:</b> {expiry_date.strftime('%Y-%m-%d')}\n"
        f"🔗 <b>لینک اتصال:</b>\n<code>{url}</code>\n\n"
        f"{subscription_block}"
        "⚠️ لطفاً این لینک را در برنامه خود وارد کنید.",
        parse_mode="HTML"
    )


async def create_panel_client(*, name: str, traffic_gb: int, inbound_id: int, expiry_days: int, limit_ip: int,
                              server_name: str, address: str):
    return await create_server_client(
        name=name,
        traffic_gb=traffic_gb,
        inbound_id=inbound_id,
        expiry_days=expiry_days,
        limit_ip=limit_ip,
        server_name=server_name,
        address=address,
    )


async def update_panel_client(*, base_url: str, username: str, password: str, inbound_id: int, client_uuid: str,
                              email: str, expiry_date: datetime, max_gb: int, enable: bool = True):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        reset_resp = session.post(f"{base_url}/panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}")
        update_resp = session.post(
            f"{base_url}/panel/api/inbounds/updateClient/{client_uuid}",
            json={
                "id": inbound_id,
                "settings": json.dumps({
                    "clients": [{
                        "id": client_uuid,
                        "email": email,
                        "enable": enable,
                        "expiryTime": int(expiry_date.timestamp() * 1000),
                        "totalGB": int(max_gb) * 1024 ** 3
                    }]
                })
            }
        )
        return reset_resp, update_resp

    return await asyncio.to_thread(_worker)


async def fetch_panel_client_traffic(*, base_url: str, username: str, password: str, client_uuid: str):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        traffic_resp = session.get(f"{base_url}/panel/api/inbounds/getClientTrafficsById/{client_uuid}")
        if not traffic_resp.ok:
            raise RuntimeError("panel_traffic_fetch_failed")
        return traffic_resp.json()

    return await asyncio.to_thread(_worker)


async def fetch_panel_inbounds(*, base_url: str, username: str, password: str):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        inbound_resp = session.get(f"{base_url}/panel/api/inbounds/list")
        if not inbound_resp.ok:
            raise RuntimeError("panel_inbounds_fetch_failed")
        return inbound_resp.json()

    return await asyncio.to_thread(_worker)


async def delete_panel_client(*, base_url: str, username: str, password: str, inbound_id: int, client_uuid: str):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        delete_resp = session.post(f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")
        if not delete_resp.ok or not delete_resp.json().get("success"):
            raise RuntimeError(delete_resp.text or "panel_delete_failed")
        return delete_resp

    return await asyncio.to_thread(_worker)


async def update_panel_client_payload(*, base_url: str, username: str, password: str, inbound_id: int,
                                      client_uuid: str, client_payload: dict):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        update_resp = session.post(
            f"{base_url}/panel/api/inbounds/updateClient/{client_uuid}",
            json={"id": inbound_id, "settings": json.dumps({"clients": [client_payload]})},
        )
        if not update_resp.ok or not update_resp.json().get("success"):
            raise RuntimeError(update_resp.text or "panel_update_failed")
        return update_resp

    return await asyncio.to_thread(_worker)


async def get_sanaei_subscription_link(*, base_url: str, username: str, password: str, client_uuid: str):
    def _worker():
        session = requests.Session()
        login_resp = session.post(f"{base_url}/login", json={"username": username, "password": password})
        if not login_resp.ok or not login_resp.json().get("success"):
            raise RuntimeError("panel_login_failed")

        response = session.get(f"{base_url}/panel/api/inbounds/getClientSubscription/{client_uuid}")
        if not response.ok:
            raise RuntimeError("subscription_fetch_failed")

        try:
            payload = response.json()
        except Exception:
            payload = {}

        for key in ("subscription_url", "url", "subUrl", "sub_url", "link"):
            value = payload.get(key)
            if value:
                return value

        obj = payload.get("obj")
        if isinstance(obj, dict):
            for key in ("subscription_url", "url", "subUrl", "sub_url", "link"):
                value = obj.get(key)
                if value:
                    return value
        if isinstance(obj, str) and obj.strip():
            return obj.strip()

        text = response.text.strip()
        if text.startswith("http"):
            return text
        return None

    return await asyncio.to_thread(_worker)


def _to_nowpayments_amount(amount_toman: int) -> float:
    if NOWPAYMENTS_PRICE_CURRENCY.lower() == "usd":
        return round(float(amount_toman) / USD_TO_TOMAN_RATE, 2)
    return round(float(amount_toman), 2)


async def create_nowpayment_invoice(amount, user_id):
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY is not configured")

    amount = int(amount)

    def _worker():
        response = requests.post(
            f"{NOWPAYMENTS_BASE_URL}/invoice",
            headers={
                "x-api-key": NOWPAYMENTS_API_KEY,
                "Content-Type": "application/json",
            },
            json={
                "price_amount": _to_nowpayments_amount(amount),
                "price_currency": NOWPAYMENTS_PRICE_CURRENCY,
                "pay_currency": NOWPAYMENTS_PAY_CURRENCY,
                "order_id": f"tg-{user_id}-{int(time.time())}",
                "order_description": f"TelegramSellBot charge for {user_id}",
            },
            timeout=REQ_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        payment_url = payload.get("invoice_url") or payload.get("payment_url")
        invoice_id = payload.get("id") or payload.get("invoice_id")
        if not payment_url or not invoice_id:
            raise RuntimeError("invalid_nowpayments_invoice_response")
        return payment_url, str(invoice_id)

    return await asyncio.to_thread(_worker)


async def check_nowpayment_status(invoice_id):
    if not NOWPAYMENTS_API_KEY:
        raise RuntimeError("NOWPAYMENTS_API_KEY is not configured")

    def _worker():
        response = requests.get(
            f"{NOWPAYMENTS_BASE_URL}/invoice/{invoice_id}",
            headers={"x-api-key": NOWPAYMENTS_API_KEY},
            timeout=REQ_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        status = (payload.get("invoice_status") or payload.get("payment_status") or payload.get("status") or "").lower()
        if status in {"finished", "confirmed", "paid"}:
            return "paid"
        return status or "waiting"

    return await asyncio.to_thread(_worker)


def find_panel_client(inbound_list: dict, inbound_id, client_uuid: str):
    for inbound in (inbound_list.get("obj", []) or []):
        if str(inbound.get("id")) != str(inbound_id):
            continue
        try:
            settings = json.loads(inbound.get("settings", "{}"))
        except Exception:
            settings = {}
        for client in settings.get("clients", []) or []:
            if client.get("id") == client_uuid:
                return client
        break
    return None


def format_purchased_plan_detail_text(*, name, status, reason_block, url, remark, purchase_date, expiry_date,
                                      used_show, max_gb, remaining_gb, up_gb, down_gb):
    exp_txt = expiry_date.strftime("%Y-%m-%d %H:%M") if expiry_date else "—"
    pur_txt = purchase_date.strftime("%Y-%m-%d %H:%M") if purchase_date else "—"
    return (
        "<b>📡 اطلاعات سرویس</b>\n"
        f"وضعیت سرویس: {status}{reason_block}\n"
        f"🌻 <b>نام سرویس:</b> <code>{name}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🖥 اطلاعات اتصال</b>\n"
        f"🔗 <b>لینک اتصال:</b>\n<code>{url}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔖 جزئیات طرح</b>\n"
        f"📦 <b>نام پلن:</b> {remark}\n"
        f"📅 <b>تاریخ خرید:</b> <code>{pur_txt}</code>\n"
        f"📆 <b>تاریخ انقضا:</b> <code>{exp_txt}</code>\n"
        f"📊 <b>مصرف:</b> <code>{used_show:.2f} / {max_gb:.2f} GB</code>\n"
        f"📉 <b>حجم باقی‌مانده:</b> <code>{remaining_gb:.2f} GB</code>\n"
        f"📤 <b>آپلود:</b> <code>{up_gb:.2f} GB</code>\n"
        f"📥 <b>دانلود:</b> <code>{down_gb:.2f} GB</code>\n"
    )


async def create_server_client(name: str, traffic_gb: int, inbound_id: int, expiry_days: int, limit_ip: int,
                               server_name: str, address: str):
    server_detail = q_one(
        "SELECT base_url, username, password FROM servers WHERE name = %s LIMIT 1", (server_name,)
    )

    if not server_detail:
        return None, None, None, None, f"❌ سرور '{server_name}' پیدا نشد."

    base_url, panel_username, panel_password = server_detail
    api_base = f"{base_url}/panel/api/inbounds"

    def _worker():
        session = requests.Session()
        login_payload = {
            "username": panel_username,
            "password": panel_password
        }
        login_resp = session.post(f"{base_url}/login", json=login_payload)
        if not login_resp.ok or not login_resp.json().get("success"):
            return None, None, None, None, "❌ ورود به پنل با خطا مواجه شد."

        total_bytes = traffic_gb * 1024 ** 3
        expiry_ms = int((datetime.now() + relativedelta(days=expiry_days)).timestamp() * 1000)
        rnd = randint(1, 500000)
        client_uuid = str(uuid.uuid4())
        try:
            email = f"@mrfox_vpn--{int(name) + rnd}"
        except Exception:
            email = f"@mrfox_vpn--{rnd}-{name}"

        new_client = {
            "id": client_uuid,
            "email": email,
            "enable": True,
            "flow": "",
            "limitIp": limit_ip,
            "totalGB": total_bytes,
            "expiryTime": expiry_ms,
            "tgId": "",
            "subId": "custom-subid",
            "reset": 0
        }

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [new_client]})
        }

        add_resp = session.post(f"{api_base}/addClient", json=payload)
        try:
            result = add_resp.json()
            if not result.get("success"):
                return None, None, None, None, f"❗️ افزودن کلاینت ناموفق بود: {result.get('msg')}"
        except Exception as e:
            return None, None, None, None, f"❗️ خطا در پاسخ API: {e}"

        inbound_resp = session.get(f"{api_base}/get/{payload['id']}")
        try:
            inbound_data = inbound_resp.json()
            protocol = inbound_data['obj']['protocol']
            port = inbound_data['obj']['port']
        except Exception:
            return None, None, None, None, "❌ خطا در دریافت تنظیمات نهایی سرور."

        url = (
            f"{protocol}://{client_uuid}@{address}:{port}"
            f"?type=tcp&path=%2F&headerType=http&security=none#{email}"
        )

        return port, client_uuid, protocol, url, "✅ ساخت کلاینت موفقیت‌آمیز بود."

    return await asyncio.to_thread(_worker)


def save_user(user_id, name, username, phone, ref_code=None):
    refered_by_id = None
    if ref_code:
        result = q_one("SELECT id FROM users WHERE refcode = %s", (ref_code,))
        if result:
            refered_by_id = result[0]

    q_exec("""
        INSERT INTO users (
            user_id, name, username, phone, created_at,
            refcode, refered_by, discount_percentage,
            freetrial, spam_info, account_status, cart_visibility
        )
        VALUES (%s, %s, %s, %s, %s, CONCAT('ref_', %s), %s, 0, FALSE, NULL, 'active', FALSE)
        ON CONFLICT (user_id) DO NOTHING
    """, (
        user_id,
        name,
        username,
        phone,
        datetime.utcnow(),
        user_id,
        refered_by_id
    ))
    result = q_one("SELECT id FROM users WHERE user_id = %s", (user_id,))
    if not result:
        return
    db_user_id = result[0]
    if refered_by_id == db_user_id:
        refered_by_id = None
        q_exec("UPDATE users SET refered_by = NULL WHERE id = %s", (db_user_id,))
    q_exec("""
        INSERT INTO wallets (user_id)
        VALUES (%s)
        ON CONFLICT (user_id) DO NOTHING
    """, (db_user_id,))


async def is_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except (TimedOut, NetworkError) as e:
        logger.warning("Channel check failed: %s", e)
        return False
    except Exception as e:
        logger.exception("Channel check unexpected error: %s", e)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("start called tg_user=%s", getattr(update.effective_user, "id", None))
    context.user_data.clear()
    clear_user_state(context)
    user = update.effective_user
    user_id = user.id
    ref_code = None
    if update.message and update.message.text:
        parts = update.message.text.strip().split()
        if len(parts) > 1 and parts[0] == "/start":
            ref_code = parts[1]
    result = q_one("SELECT * FROM users WHERE user_id = %s", (user_id,))

    if result:
        if await is_member(user_id, context):
            await update.message.reply_text("""
            🎉 خوش اومدی!  
            """)
            await main_menu(update, context)
        else:
            await send_join_prompt(update)
        return
    if ref_code:
        context.user_data["ref_code_used"] = ref_code
    button = KeyboardButton("📱 ارسال شماره", request_contact=True)
    markup = ReplyKeyboardMarkup([[button]], resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text("📲 لطفا شماره خود را ارسال کنید با استفاده از گزینه ی پایین", reply_markup=markup)


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_contact tg_user=%s", getattr(update.effective_user, "id", None))
    user = update.effective_user
    contact = update.message.contact

    # Ensure it's their own contact
    if user.id != contact.user_id:
        await update.message.reply_text("❗️ لطفاً شماره خودتان را ارسال کنید.")
        return

    phone = normalize_iran_phone(contact.phone_number)
    if phone is None:
        await update.message.reply_text("❌ فقط شماره‌های ایرانی مجاز هستند.")
        return

    ref_code = context.user_data.get("ref_code_used")

    save_user(
        user_id=user.id,
        name=user.full_name,
        username=user.username,
        phone=phone,  # store canonical format
        ref_code=ref_code,
    )

    msg = await update.message.reply_text(
        "✅ شماره با موفقیت ثبت شد.",
        reply_markup=ReplyKeyboardRemove()
    )
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)
    await send_join_prompt(update)


async def send_join_prompt(update: Update):
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💢 چک کردن", callback_data="check_join")
    ]])
    await update.message.reply_text(
        f"""
        🎈 لطفا در چنل زیر عضو بشید 🎈
        {channel_username}
        """,
        reply_markup=keyboard
    )


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_id = user.id

    if await is_member(user_id, context):
        msg = await query.edit_message_text("🎉 خوش آمدید، حضور شما تایید شد")
        context.user_data.setdefault("bot_messages", []).append(msg.message_id)
        await main_menu(update, context)
        result = q_one("SELECT id, refered_by FROM users WHERE user_id = %s", (user_id,))
        refered_by = None
        if result:
            db_user_id, refered_by_id = result
            if refered_by_id:
                ref_data = q_one("SELECT name, username FROM users WHERE id = %s", (refered_by_id,))
                if ref_data:
                    ref_name, ref_username = ref_data
                    refered_by = f"{ref_name or ''} (@{ref_username})" if ref_username else ref_name or "نامشخص"
        name = user.full_name
        username = f"@{user.username}" if user.username else "ندارد"
        text = (
            f"🟢 <b>کاربر جدید عضو شد</b>\n"
            f"📛 نام: {name}\n"
            f"🔗 یوزرنیم: {username}\n"
            f"🆔 آیدی عددی: <code>{user.id}</code>\n"
        )
        if refered_by:
            text += f"🙋‍♂️ <b>معرف:</b> {refered_by}"

        if refered_by_id:
            ref_tg = q_one("SELECT user_id FROM users WHERE id = %s", (refered_by_id,))
            if ref_tg and ref_tg[0]:
                try:
                    await context.bot.send_message(
                        chat_id=ref_tg[0],
                        text=(
                            "🎉 یک نفر با لینک دعوت شما ثبت‌نام کرد و عضو ربات شد!\n"
                            f"👤 {name}\n"
                            f"🆔 {user.id}"
                        )
                    )
                except Exception as e:
                    logger.warning("Failed to notify referrer: %s", e)

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode="HTML"
        )
    else:
        await query.answer(
            "❗️ شما هنوز در چنل عضو نشدید، لطفا عضو بشید و سپس گزینه ی چک کردن را بزنید ❗️",
            show_alert=True
        )


def is_admin(user_id):
    return q_one("SELECT 1 FROM admins WHERE user_id = %s", (user_id,)) is not None


async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    old_messages = context.user_data.get("bot_messages", [])
    for msg_id in old_messages:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except:
            pass

    profile_button = KeyboardButton("👤 پروفایل")
    buy_server_button = KeyboardButton("💰 خرید سرور نیم بها")
    guid_button = KeyboardButton("🎓 آموزش اتصال")
    wallet_button = KeyboardButton("💼 کیف پول")
    support_button = KeyboardButton("🛠️ پشتیبانی")
    admin_panel_button = KeyboardButton("⚙️ پنل ادمین")
    buttons = [
        [profile_button, buy_server_button],
        [guid_button, wallet_button],
        [support_button]
    ]

    if is_admin(user_id):
        buttons.append([admin_panel_button])

    markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    await context.bot.send_message(chat_id=user_id, text="""
    برای مدیریت یا خرید سرویس فقط کافیه یکی از دکمه‌های زیر رو بزنی 👇
    """, reply_markup=markup)


async def handle_menu_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_menu_selection tg_user=%s", getattr(update.effective_user, "id", None))
    state = get_user_state(context)

    if state == STATE_AWAITING_ADMIN_USER_MESSAGE:
        target_tg_id = context.user_data.get("target_user_tg_id")

        try:
            await context.bot.copy_message(
                chat_id=target_tg_id,
                from_chat_id=update.effective_chat.id,
                message_id=update.message.message_id
            )
            await update.message.reply_text("✅ پیام/مدیا با موفقیت ارسال شد.")
        except Exception as e:
            await update.message.reply_text(f"❌ ارسال ناموفق بود: {e}")

        clear_user_state(context)
        context.user_data.pop("target_user_internal_id", None)
        context.user_data.pop("target_user_tg_id", None)

        await show_admin_panel(update, context)
        return
    if state == STATE_AWAITING_EDIT_SERVER_VALUE:
        await handle_text_input_edit_server(update, context)
        return
    if state == STATE_AWAITING_EDIT_PLAN_VALUE:
        await handle_text_input_edit_plan(update, context)
        return
    elif state == STATE_AWAITING_ADMIN_REPLY:
        await handle_confirm_admin_message(update, context)
        return
    if get_timed_value(context, "server_plans"):
        await handle_add_server_plan_input(update, context)
        return
    if get_timed_value(context, "add_category"):
        await handle_category_input(update, context)
        return
    if get_timed_value(context, "awaiting_confirm_plan"):
        await handle_confirm_buy_plan(update, context)
        return
    if get_timed_value(context, "awaiting_change_name_purchased"):
        await handle_change_name_purchased_plan(update, context)
        return
    if get_timed_value(context, "awaiting_change_balance"):
        await handle_user_change_balance(update, context)
        return
    if get_timed_value(context, "search_by"):
        await handle_search_user(update, context)
        return
    if get_timed_value(context, "awaiting_change_discount"):
        await handle_user_discount_percentage(update, context)
        return
    text = update.message.text
    if text == "↩️ بازگشت":
        if state == STATE_BACK_ADMIN_PANEL:
            await main_menu(update, context)
        elif state == STATE_BACK_SETUP_SERVERS:
            await show_admin_panel(update, context, message="🌐 شما به پنل ادمین برگشتید")
        elif state == STATE_BACK_BUY_SERVER_PANEL:
            await main_menu(update, context)
        elif state == STATE_BACK_PROFILE_PANEL:
            await main_menu(update, context)
        elif state is None:
            await main_menu(update, context)
        return
    if text == "⚙️ پنل ادمین":
        await show_admin_panel(update, context)
        return
    elif text == "💰 خرید سرور نیم بها":
        await show_buy_server_panel(update, context)
        return
    elif text == "👤 پروفایل":
        await show_profile_panel(update, context)
        return
    elif text == "🎓 آموزش اتصال":
        await update.effective_message.reply_text("فعلا این قسمت راه اندازی نشده")
    elif text == "💼 کیف پول":
        await show_wallet_panel(update, context)
        return
    if state in [STATE_BACK_ADMIN_PANEL, STATE_BACK_SETUP_SERVERS]:
        await handle_admin_panel(update, context)
        return
    elif text == "🛠️ پشتیبانی":
        await handle_support_panel(update, context)
        return
    if state == STATE_BACK_PROFILE_PANEL:
        await handle_profile_panel(update, context)
        return
    if state == STATE_BACK_BUY_SERVER_PANEL:
        await handle_buy_server(update, context)
        return
    if state == "server_edit_text_input":
        return
    if state == STATE_AWAITING_SUPPORT_MESSAGE:
        await confirm_support_message_panel(update, context)
        return
    if state == STATE_AWAITING_WALLET_AMOUNT:
        await handle_wallet_amount(update, context)
        return
    if state == STATE_AWAITING_WALLET_RECEIPT:
        await update.message.reply_text("⚠️ لطفاً عکس رسید را ارسال کنید. برای لغو: /start")
        return
    if state == STATE_AWAITING_PROMO_PERCENT:
        await handle_promo_percent_input(update, context)
        return

    if state == STATE_AWAITING_PROMO_END:
        await handle_promo_end_input(update, context)
        return

    await update.message.reply_text("‼️ دستور نامعتبر است. برای برگشت به منوی اصلی از /start استفاده کنید.")


async def show_wallet_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("show_wallet_panel tg_user=%s", getattr(update.effective_user, "id", None))
    user_tg = update.effective_user.id
    wallet_row = q_one("""
        SELECT w.balance
        FROM wallets w
        JOIN users u ON u.id = w.user_id
        WHERE u.user_id = %s
    """, (user_tg,))
    wallet_balance = int(wallet_row[0]) if wallet_row else 0

    set_user_state(context, STATE_AWAITING_WALLET_AMOUNT)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ 200,000 تومان", callback_data="wallet_amount_200000"),
         InlineKeyboardButton("🔥 500,000 تومان", callback_data="wallet_amount_500000")],
        [InlineKeyboardButton("💎 1,000,000 تومان", callback_data="wallet_amount_1000000")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_main_menu")]
    ])
    await update.effective_message.reply_text(
        "💼 <b>کیف پول شما</b>\n\n"
        f"💰 <b>موجودی:</b> {wallet_balance:,} تومان\n"
        "عدد شارژ را بفرستید یا یکی از مبلغ‌های آماده را انتخاب کنید.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_wallet_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """وقتی state=awaiting_wallet_amount است و کاربر عدد می‌فرستد"""
    if get_user_state(context) != STATE_AWAITING_WALLET_AMOUNT:
        await update.message.reply_text("⛔️ این مرحله منقضی شده است. دوباره از /start شروع کنید.")
        return
    raw = (update.message.text or "").strip().replace(",", "")
    if not raw.isdigit():
        await update.message.reply_text("⚠️ لطفاً فقط عدد وارد کنید. مثال: 25000")
        return

    amount = int(raw)
    if amount < 20000 or amount > 500000:
        await update.message.reply_text("⚠️ مبلغ باید بین ۲۰٬۰۰۰ تا ۵۰۰٬۰۰۰ تومان باشد.")
        return

    context.user_data["charge_amount"] = amount
    set_user_state(context, STATE_AWAITING_WALLET_CONFIRM)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید مبلغ", callback_data="wallet_confirm")],
        [InlineKeyboardButton("❌ لغو", callback_data="wallet_cancel")]
    ])
    await update.message.reply_text(
        f"مبلغ انتخابی: {amount:,} تومان 💰\nآیا تایید می‌کنید؟",
        reply_markup=kb
    )


async def handle_wallet_confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """کلیک روی تایید/لغو مبلغ"""
    q = update.callback_query
    await q.answer()

    if q.data == "wallet_cancel":
        context.user_data.clear()
        await q.edit_message_text("❌ عملیات شارژ لغو شد.")
        return

    if get_user_state(context) != STATE_AWAITING_WALLET_CONFIRM:
        await q.edit_message_text("⛔️ نشست نامعتبر. دوباره /start را بزنید.")
        context.user_data.clear()
        return

    user_tg = q.from_user.id
    username = q.from_user.username or None
    amount = context.user_data.get("charge_amount")
    if not amount:
        await q.edit_message_text("⛔️ نشست نامعتبر. دوباره /start را بزنید.")
        context.user_data.clear()
        return

    if is_admin(user_tg):
        q_exec("""
            INSERT INTO users (user_id, username)
            VALUES (%s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_tg, username))
        internal_user_id = q_one("SELECT id FROM users WHERE user_id=%s", (user_tg,))[0]
        unique_ref = f"manual:{internal_user_id}:{int(time.time())}"
        tx_id = q_one("""
            INSERT INTO wallet_transactions
                (user_id, amount, type, method, description, status, receipt_file_id, unique_ref)
            VALUES
                (%s, %s, 'increase', 'manual_deposit', 'در انتظار واریز کاربر', 'pending', NULL, %s)
            RETURNING id
        """, (internal_user_id, amount, unique_ref))[0]
        context.user_data["pending_tx_id"] = tx_id
        set_user_state(context, STATE_AWAITING_WALLET_RECEIPT)

        await q.edit_message_text(
            f"✅ مبلغ {amount:,} تومان ثبت شد.\n"
            f"لطفاً مبلغ را به شماره کارت زیر واریز کنید و سپس **عکس رسید** را ارسال کنید:\n\n"
            f"💳 {CARD_NUMBER}\n\n"
            "به اسم: سهند یوسف جانی\n "
            "برای لغو، /start را ارسال کنید."
        )
        return

    set_user_state(context, STATE_AWAITING_NOWPAYMENT)
    await q.edit_message_text(
        f"💎 شارژ {amount:,} تومان آماده است.\n"
        "برای ساخت لینک پرداخت روی دکمه زیر بزنید.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 پرداخت با NowPayments", callback_data="nowpayment_wallet")],
            [InlineKeyboardButton("❌ لغو", callback_data="wallet_cancel")]
        ])
    )


async def handle_wallet_amount_preset_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    amount = int(query.data.rsplit("_", 1)[-1])
    context.user_data["charge_amount"] = amount
    set_user_state(context, STATE_AWAITING_WALLET_CONFIRM)
    await query.edit_message_text(
        f"مبلغ انتخابی: {amount:,} تومان 💰\nآیا تایید می‌کنید؟",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ تایید مبلغ", callback_data="wallet_confirm")],
            [InlineKeyboardButton("❌ لغو", callback_data="wallet_cancel")]
        ])
    )


async def handle_nowpayment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    tg_user_id = query.from_user.id

    if data == "nowpayment_wallet":
        amount = int(context.user_data.get("charge_amount") or 0)
        if amount <= 0:
            await query.edit_message_text("❌ مبلغ معتبر برای پرداخت پیدا نشد.")
            return
        source = "wallet"
        source_id = 0
    elif data.startswith("nowpayment_buy_"):
        source = "buy"
        source_id = int(data.rsplit("_", 1)[-1])
        amount = int(context.user_data.get("plan_price") or 0)
        if amount <= 0:
            await query.edit_message_text("❌ مبلغ طرح برای پرداخت پیدا نشد.")
            return
    elif data.startswith("check_nowpayment_"):
        invoice_id = data.removeprefix("check_nowpayment_")
        status = await check_nowpayment_status(invoice_id)
        if status != "paid":
            await query.answer(f"وضعیت فعلی: {status}", show_alert=True)
            return

        pending = q_one("""
            SELECT id, user_id, amount, status, description
            FROM wallet_transactions
            WHERE unique_ref = %s
        """, (f"nowpayments:{invoice_id}",))
        if not pending:
            await query.answer("تراکنش یافت نشد.", show_alert=True)
            return
        tx_id, internal_user_id, amount, tx_status, description = pending
        if tx_status == "success":
            await query.answer("این پرداخت قبلاً ثبت شده است.", show_alert=True)
            return

        db_conn = pool.getconn()
        try:
            with db_conn:
                with db_conn.cursor() as cur:
                    cur.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (internal_user_id,))
                    wallet_row = cur.fetchone()
                    if not wallet_row:
                        raise ValueError("wallet_not_found")
                    new_balance = int(wallet_row[0]) + int(amount)
                    cur.execute(
                        "UPDATE wallets SET balance = %s, updated_at = NOW() WHERE user_id = %s",
                        (new_balance, internal_user_id)
                    )
                    cur.execute(
                        "UPDATE wallet_transactions SET status = 'success', description = %s WHERE id = %s",
                        (f"{description} | paid", tx_id)
                    )
        finally:
            pool.putconn(db_conn)

        clear_user_state(context)
        await query.edit_message_text("✅ پرداخت تایید شد و موجودی کیف پول شما شارژ شد.")
        return
    else:
        return

    q_exec("""
        INSERT INTO users (user_id, username)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO NOTHING
    """, (tg_user_id, query.from_user.username or None))
    internal_user_id = q_one("SELECT id FROM users WHERE user_id = %s", (tg_user_id,))[0]
    payment_url, invoice_id = await create_nowpayment_invoice(amount, tg_user_id)
    q_exec("""
        INSERT INTO wallet_transactions (user_id, amount, type, method, description, status, unique_ref, created_at)
        VALUES (%s, %s, 'increase', 'nowpayments', %s, 'pending', %s, NOW())
    """, (
        internal_user_id,
        amount,
        f"NOWPayments invoice for {source}:{source_id}",
        f"nowpayments:{invoice_id}",
    ))
    set_user_state(context, STATE_AWAITING_NOWPAYMENT)
    await query.edit_message_text(
        f"💎 لینک پرداخت شما آماده است.\n\n💰 مبلغ: {amount:,} تومان",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✨ باز کردن لینک پرداخت", url=payment_url)],
            [InlineKeyboardButton("🔄 بررسی وضعیت پرداخت", callback_data=f"check_nowpayment_{invoice_id}")]
        ])
    )


async def handle_wallet_receipt_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # فقط وقتی منتظر رسید هستیم
    if get_user_state(context) != STATE_AWAITING_WALLET_RECEIPT:
        return

    if not update.message.photo:
        await update.message.reply_text("⚠️ لطفاً عکس رسید را ارسال کنید.")
        return

    tx_id = context.user_data.get("pending_tx_id")
    if not tx_id:
        await update.message.reply_text("⛔️ نشست نامعتبر. دوباره /start را بزنید.")
        context.user_data.clear()
        return

    file_id = update.message.photo[-1].file_id
    caption = update.message.caption or ""

    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE wallet_transactions
                    SET receipt_file_id=%s, description=%s
                    WHERE id=%s AND status='pending'
                    """,
                    (file_id, f"رسید کاربر: {caption}", tx_id)
                )

                if cur.rowcount == 0:
                    await update.message.reply_text("ℹ️ این تراکنش قبلاً بررسی شده یا معتبر نیست.")
                    return
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در ثبت رسید: {e}")
        return
    finally:
        pool.putconn(db_conn)

    # اطلاعات برای ارسال به ادمین
    tg_id = update.effective_user.id
    username = update.effective_user.username or "بدون نام کاربری"
    amount = context.user_data.get("charge_amount")

    kb_admin = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تایید واریز", callback_data=f"wallet_appr_{tx_id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"wallet_rej_{tx_id}")
    ]])

    await context.bot.send_photo(
        chat_id=ADMIN_ID,
        photo=file_id,
        caption=(
            f"🧾 رسید واریز جدید\n"
            f"TX #{tx_id}\n"
            f"👤 User: @{username} | {tg_id}\n"
            f"💰 Amount: {amount:,} تومان\n"
            f"🕐 Status: pending"
        ),
        reply_markup=kb_admin
    )

    await update.message.reply_text("✅ رسید دریافت شد. در حال بررسی توسط ادمین.")
    set_user_state(context, STATE_IDLE)


async def handle_wallet_admin_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_wallet_admin_cb admin=%s", getattr(update.effective_user, "id", None))
    q = update.callback_query
    await q.answer()
    callback = parse_callback(q.data)

    approve = callback.get("action") == "appr"
    reject = callback.get("action") == "rej"
    if not (approve or reject):
        return

    tx_id = callback["id"]

    db_conn = pool.getconn()
    telegram_chat_id = None
    amount = None

    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, user_id, amount, status
                    FROM wallet_transactions
                    WHERE id=%s
                    FOR UPDATE
                    """,
                    (tx_id,)
                )
                row = cur.fetchone()
                if not row:
                    await q.edit_message_caption(caption="⛔️ تراکنش یافت نشد.")
                    return

                _id, internal_user_id, amount, status = row
                if status != "pending":
                    await q.edit_message_caption(caption=f"ℹ️ این تراکنش قبلاً بررسی شده ({status}).")
                    return

                cur.execute("SELECT user_id FROM users WHERE id=%s", (internal_user_id,))
                res = cur.fetchone()
                if not res:
                    await q.edit_message_caption(caption="⚠️ کاربر مرتبط یافت نشد.")
                    return
                telegram_chat_id = res[0]

                if approve:
                    cur.execute("SELECT 1 FROM wallets WHERE user_id=%s", (internal_user_id,))
                    if not cur.fetchone():
                        cur.execute(
                            "INSERT INTO wallets (user_id, balance) VALUES (%s, 0)",
                            (internal_user_id,)
                        )
                    cur.execute(
                        "UPDATE wallets SET balance = balance + %s WHERE user_id = %s",
                        (amount, internal_user_id)
                    )
                    cur.execute(
                        "UPDATE wallet_transactions SET status='success' WHERE id=%s",
                        (tx_id,)
                    )

                else:
                    cur.execute(
                        "UPDATE wallet_transactions SET status='rejected' WHERE id=%s",
                        (tx_id,)
                    )

    except Exception as e:
        await q.edit_message_caption(caption=f"❌ خطا در پردازش: {e}")
        return

    finally:
        pool.putconn(db_conn)

    if approve:
        try:
            await context.bot.send_message(
                telegram_chat_id,
                f"✅ شارژ شما تایید شد. مبلغ {amount:,} تومان به کیف شما افزوده شد."
            )
            await q.edit_message_caption(caption=f"✅ تایید شد — TX #{tx_id}")
        except Exception as e:
            await q.edit_message_caption(
                caption=f"✅ تایید شد — TX #{tx_id}\n(⚠️ ارسال پیام به کاربر ناموفق: {e})"
            )
    else:
        try:
            await context.bot.send_message(
                telegram_chat_id,
                "❌ واریز شما رد شد. در صورت سوال به پشتیبانی پیام دهید."
            )
        except Exception:
            pass
        await q.edit_message_caption(caption=f"❌ رد شد — TX #{tx_id}")


async def handle_support_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    loading_msg = await update.effective_message.reply_text(
        "⌛ لطفا صبر کنید...",
        reply_markup=ReplyKeyboardRemove()
    )
    await asyncio.sleep(0.5)
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
    except:
        pass
    set_user_state(context, STATE_AWAITING_SUPPORT_MESSAGE)
    await update.message.reply_text("✉️ لطفاً پیام خود را ارسال کنید تا به ادمین ارسال شود.")


async def confirm_support_message_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = sanitize_text_input(update.message.text, max_len=1500, field_name="support_message")
    user_id = update.effective_user.id
    username = update.effective_user.username or "بدون نام کاربری"
    full_name = update.effective_user.full_name
    msg_id = q_one(
        """
        INSERT INTO support_messages (user_id, username, message_text)
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (user_id, username, text)
    )[0]
    reply_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✍️ پاسخ دادن", callback_data=f"reply_to_{msg_id}")
    ]])

    await context.bot.send_message(
        chat_id=ADMIN_ID,
        text=f"📩 پیام جدید از {full_name} (@{username}):\n\n{text}",
        reply_markup=reply_markup
    )

    await update.message.reply_text("✅ پیام شما با موفقیت به ادمین ارسال شد.")
    clear_user_state(context)
    await main_menu(update, context)


async def show_buy_server_panel(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                message="💎 خوش آمدبد به پنل خرید سرور"):
    context.user_data.clear()
    set_user_state(context, STATE_BACK_BUY_SERVER_PANEL)
    buttons = [[InlineKeyboardButton("💎 خرید سرور", callback_data="buy_panel_open")]]
    config = q_one("""
        SELECT t.is_active, s.name, t.inbound_id, t.traffic_gb, t.duration_days
        FROM test_server_config t
        JOIN servers s ON t.server_id = s.id
        LIMIT 1
    """)

    if config and config[0]:
        buttons[0].append(InlineKeyboardButton("🎁 سرور تست", callback_data="buy_panel_test"))
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="return_main_menu")])
    markup = InlineKeyboardMarkup(buttons)
    if update.message:
        await update.message.reply_text(message, reply_markup=markup)
    elif update.callback_query:
        await update.callback_query.message.reply_text(message, reply_markup=markup)


async def show_profile_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    set_user_state(context, STATE_BACK_PROFILE_PANEL)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✨ اطلاعات کاربر", callback_data="profile_user_info"),
         InlineKeyboardButton("💎 کانفیگ‌های من", callback_data="return_user_purchased")],
        [InlineKeyboardButton("🔥 بررسی سرور دلخواه", callback_data="profile_custom_server"),
         InlineKeyboardButton("👥 لینک دعوت من", callback_data="profile_referral")],
        [InlineKeyboardButton("🎁 درخواست جایزه", callback_data="profile_reward")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_main_menu")]
    ])
    if update.message:
        await update.message.reply_text("🔧 لطفاً یکی از گزینه‌های زیر را انتخاب کنید:", reply_markup=keyboard)
    elif update.callback_query:
        await update.callback_query.message.reply_text("🔧 لطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
                                                       reply_markup=keyboard)


async def handle_test_server_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_username = update.effective_user.username
    result = q_one("SELECT freetrial FROM users WHERE user_id = %s", (user_id,))
    if not result or result[0]:
        await update.effective_message.reply_text("⚠️ شما قبلاً از سرور تست استفاده کرده‌اید.")
        await show_buy_server_panel(update, context)
        return

    config = q_one("""
        SELECT t.is_active, s.name, t.inbound_id, t.traffic_gb, t.duration_days
        FROM test_server_config t
        JOIN servers s ON t.server_id = s.id
        LIMIT 1
    """)
    if not config:
        await update.effective_message.reply_text("❌ پیکربندی تست یافت نشد.")
        return

    is_active, server_name, inbound_id, traffic_gb, duration_days = config
    if not is_active:
        await update.effective_message.reply_text("❌ سرور تست فعلاً غیرفعال است.")
        return

    server_row = q_one("SELECT address, base_url, username, password FROM servers WHERE name = %s LIMIT 1", (server_name,))
    if not server_row:
        await context.bot.send_message(
            ADMIN_ID,
            f"⚠️ تلاش برای دریافت سرور تست توسط @{user_username or 'نامشخص'} (ID: {user_id}) — سرور یافت نشد.",
            parse_mode="HTML"
        )
        return

    address, base_url, server_username, server_password = server_row
    port, client_uuid, protocol, url, error = await create_panel_client(
        name=str(user_id),
        traffic_gb=traffic_gb,
        expiry_days=duration_days,
        inbound_id=inbound_id,
        limit_ip=1,
        server_name=server_name,
        address=address
    )

    subscription_url = None
    if error == "✅ ساخت کلاینت موفقیت‌آمیز بود.":
        try:
            subscription_url = await get_sanaei_subscription_link(
                base_url=base_url,
                username=server_username,
                password=server_password,
                client_uuid=client_uuid,
            )
        except Exception:
            logger.exception("failed to fetch test subscription link")

    await context.bot.send_message(
        ADMIN_ID,
        f"""
⚠️ <b>وضعیت ساخت سرور تست</b>

👤 <b>کاربر:</b> @{user_username or 'نامشخص'}
🆔 <b>آیدی عددی:</b> <code>{user_id}</code>
📄 <b>وضعیت:</b> {error}
""", parse_mode="HTML"
    )

    if error != "✅ ساخت کلاینت موفقیت‌آمیز بود.":
        await update.effective_message.reply_text(
            "❌ دریافت اکانت تست با مشکل مواجه شد.\n\n💬 لطفاً با پشتیبانی در ارتباط باشید.")
        return

    q_exec("UPDATE users SET freetrial = TRUE WHERE user_id = %s", (user_id,))
    await send_config_with_qr(
        update,
        context,
        url=url,
        max_gb=traffic_gb,
        duration_days=duration_days,
        expiry_date=datetime.now() + timedelta(days=duration_days),
        prefix_text="🎉 <b>سرور تست شما با موفقیت ساخته شد!</b>",
        subscription_url=subscription_url,
    )


async def handle_buy_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    user_username = update.effective_user.username

    loading_msg = await update.message.reply_text("⏳ در حال پردازش...", reply_markup=ReplyKeyboardRemove())
    await asyncio.sleep(0.4)
    try:
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
    except:
        pass

    if text == '🎁 دریافت سرور تست':
        await handle_test_server_request(update, context)
        return

    if text == "💰 خرید سرور":
        if user_id == ADMIN_ID:
            await show_categories_panel(update, context)
        else:
            update.message.reply_text("خرید متوقف شده")


async def show_categories_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    categories_list = q_all("SELECT id, name, emoji FROM categories")

    buttons = []
    for cat_id, cat_name, cat_emoji in categories_list:
        buttons.append([
            InlineKeyboardButton(f"{cat_name} {cat_emoji}", callback_data=f"user_view_category_{cat_id}")
        ])
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="user_back")])

    keyboard = InlineKeyboardMarkup(buttons)

    text = "<b>📦 دسته‌بندی‌های طرح‌های فروش:</b>\n\nلطفاً یکی از دسته‌ها را انتخاب کنید."

    try:
        if update.message:
            msg = await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        elif update.callback_query:
            msg = await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise

    history = context.user_data.setdefault("state_history", [])
    if not history or history[-1] != "buy_panel":
        history.append("buy_panel")


async def handle_user_view_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_user_view_category_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    cat_id = callback["id"]

    server_plans = q_all("""
        SELECT p.id, p.traffic_gb, p.duration_days, p.price, s.name, s.current_users, s.max_users
        FROM server_plans p
        JOIN servers s ON p.server_id = s.id
        WHERE p.category_id = %s
        ORDER BY p.duration_days ASC, p.traffic_gb ASC
    """, (cat_id,))

    buttons = []
    has_available_plans = False
    if server_plans:
        for plan_id, traffic_gb, duration_days, price, name, current_users, max_users in server_plans:
            if current_users >= max_users:
                continue
            has_available_plans = True
            label = f"💠 {traffic_gb} گیگ ⏳ {duration_days} روزه 💳 {price} تومان"
            buttons.append([
                InlineKeyboardButton(label, callback_data=f"user_buy_plan_{plan_id}")
            ])
        if has_available_plans:
            message_text = (
                "<b>📦 طرح‌های فروش موجود:</b>\n"
                "─────────────────────────────\n"
                "🛒 برای خرید، یکی از طرح‌ها را انتخاب کنید."
            )
        else:
            message_text = (
                "<b>❗️ در حال حاضر هیچ طرح فعالی در این دسته وجود ندارد.</b>\n"
                "⚠️ ممکن است ظرفیت سرورها پر شده باشد.\n"
                "📞 لطفاً با پشتیبانی تماس بگیرید."
            )
    else:
        message_text = "<b>❗️هیچ طرحی موجود نیست.</b>"

    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="user_back")])
    keyboard = InlineKeyboardMarkup(buttons)

    try:
        msg = await query.edit_message_text(
            text=message_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise
    context.user_data.setdefault("state_history", []).append("category_view")


async def handle_back_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    history = context.user_data.get("state_history", [])
    if history:
        history.pop()
    if history:
        last_state = history[-1]
        if last_state == "buy_panel":
            await show_categories_panel(update, context)
        elif last_state == "category_view":
            await handle_buy_server(update, context)
    else:
        await show_buy_server_panel(update, context)
    context.user_data.pop("plan_price", None)
    context.user_data.pop("wallet_balance", None)


async def handle_user_buy_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_user_buy_plan_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    callback = parse_callback(query.data)
    plan_id = callback["id"]
    plan_detail = q_one("""
    SELECT traffic_gb, duration_days, price FROM server_plans WHERE id = %s
    """, (plan_id,))
    plan_traffic, plan_days, plan_price = plan_detail
    user_row = q_one("SELECT id, discount_percentage FROM users WHERE user_id = %s", (user_id,))
    if not user_row:
        await query.edit_message_text("❗️ کاربر یافت نشد.")
        return
    db_user_id, user_discount = user_row
    wallet_row = q_one("SELECT balance FROM wallets WHERE user_id = %s", (db_user_id,))
    user_wallet = wallet_row[0] if wallet_row else 0
    admin_user = is_admin(user_id)
    final_price = plan_price
    discount_amount = 0

    if admin_user:
        final_price = 0
    elif user_discount:
        discount_amount = int(plan_price * user_discount / 100)
        final_price = plan_price - discount_amount

    message_lines = [
        "🧾 <b>مشخصات طرح انتخابی شما:</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━",
        f"📦 <b>حجم:</b> {plan_traffic}GB",
        f"⏳ <b>مدت اعتبار:</b> {plan_days} روز",
        f"💳 <b>قیمت اصلی:</b> {plan_price:,} تومان",
        f"💰 <b>موجودی کیف پول شما:</b> {user_wallet:,} تومان"
    ]

    if admin_user:
        message_lines.append("👑 <b>خرید برای ادمین رایگان است.</b>")
    elif user_discount:
        message_lines.append(f"🎁 <b>تخفیف ({user_discount}%):</b> {discount_amount:,} تومان")
        message_lines.append(f"💸 <b>قیمت نهایی پس از تخفیف:</b> {final_price:,} تومان")

    message_lines.append("━━━━━━━━━━━━━━━━━━━━━━━\n")
    message_text = "\n".join(message_lines)
    buttons = []
    if admin_user or user_wallet >= final_price:
        buttons = [
            [InlineKeyboardButton("✅ تایید خرید", callback_data=f"confirm_buy_plan_{plan_id}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="user_back")]
        ]
        message_text += ("✅ <b>می‌توانید خرید را تایید کنید.</b>")
    else:
        message_text += ("❗️ <b>موجودی کیف پول کافی نیست. ابتدا آن را شارژ کنید.</b>")
        buttons = [
            [InlineKeyboardButton("💎 پرداخت با NowPayments", callback_data=f"nowpayment_buy_{plan_id}")],
            [InlineKeyboardButton("💼 شارژ کیف پول", callback_data="go_to_wallet")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="user_back")]
        ]
    context.user_data['plan_price'] = final_price
    context.user_data['wallet_balance'] = user_wallet
    try:
        msg = await query.edit_message_text(
            text=message_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def handle_confirm_buy_plan_callback(update: Update, context: ContextTypes):
    logger.info("handle_confirm_buy_plan_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    user_id = update.effective_user.id
    plan_id = callback["id"]
    set_timed_value(context, "awaiting_confirm_plan", True)
    context.user_data["selected_plan_id"] = plan_id
    if callback["name"] == "confirm_buy_plan":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "📝 <b>لطفاً یک نام دلخواه برای اشتراک خود وارد کنید:</b>\n"
                "مثال: <code>internet123</code> یا <code>myvpn</code>",
                parse_mode="HTML"
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise


async def handle_confirm_buy_plan(update: Update, context: ContextTypes):
    logger.info("handle_confirm_buy_plan tg_user=%s", getattr(update.effective_user, "id", None))
    purchased_plan_name = (update.message.text or "").strip()
    if not purchased_plan_name:
        await update.message.reply_text("❗ لطفاً یک نام معتبر وارد کنید.")
        return

    telegram_user_id = update.effective_user.id
    user_username = update.effective_user.username
    plan_id = context.user_data.get("selected_plan_id")
    if not plan_id:
        await update.message.reply_text("❗️خطایی رخ داد. لطفاً دوباره اقدام کنید.")
        return

    plan_row = q_one(
        """
        SELECT
            u.id, sp.inbound_id, sp.traffic_gb, sp.duration_days, sp.server_id, sp.price,
            s.name, s.address, s.base_url, s.username, s.password
        FROM users u
        JOIN server_plans sp ON sp.id = %s
        JOIN servers s ON s.id = sp.server_id
        WHERE u.user_id = %s
        """,
        (plan_id, telegram_user_id),
    )
    if not plan_row:
        await update.message.reply_text("❌ کاربر یا طرح مورد نظر یافت نشد.")
        return

    (
        db_user_id, inbound_id, traffic_gb, duration_days, server_id, server_plan_price,
        server_name, server_address, base_url, server_username, server_password
    ) = plan_row
    admin_user = is_admin(telegram_user_id)
    purchase_date = datetime.now()
    expiry_date = purchase_date + timedelta(days=int(duration_days))
    discount_percentage = 0.0
    final_price = int(server_plan_price)
    new_balance = 0
    purchased_plan_id = None
    client_uuid = None
    port = None
    protocol = None
    url = None
    base_gb = int(traffic_gb)
    bonus_gb = 0
    max_gb = base_gb
    subscription_url = None

    port, client_uuid, protocol, url, error = await create_panel_client(
        name=str(purchased_plan_name),
        traffic_gb=base_gb,
        expiry_days=duration_days,
        inbound_id=inbound_id,
        limit_ip=2,
        server_name=server_name,
        address=server_address,
    )
    if error != "✅ ساخت کلاینت موفقیت‌آمیز بود.":
        await context.bot.send_message(
            ADMIN_ID,
            f"""
⚠️ <b>گزارش ساخت اکانت جدید (ناموفق)</b>
━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>کاربر:</b> @{user_username or 'نامشخص'}
🆔 <b>آیدی عددی:</b> <code>{telegram_user_id}</code>
🏷 <b>نام کانفیگ (کاربر):</b> <code>{purchased_plan_name}</code>

📦 <b>طرح انتخابی:</b> <code>💠 {traffic_gb} گیگ ⏳ {duration_days} روزه 💳 {server_plan_price}</code>
🌐 <b>سرور:</b> {server_name}
🆔 <b>Inbound ID:</b> {inbound_id}

📄 <b>وضعیت:</b> {error}
━━━━━━━━━━━━━━━━━━━━━━━
""",
            parse_mode="HTML"
        )
        await update.message.reply_text("❌ دریافت اکانت با مشکل مواجه شد.\n\n💬 لطفاً با پشتیبانی در ارتباط باشید.")
        return

    try:
        subscription_url = await get_sanaei_subscription_link(
            base_url=base_url,
            username=server_username,
            password=server_password,
            client_uuid=client_uuid,
        )
    except Exception:
        logger.exception("failed to fetch buy subscription link")

    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                if admin_user:
                    final_price, base_gb, bonus_gb, max_gb = 0, int(traffic_gb), 0, int(traffic_gb)
                    wallet_row = q_one_tx(cur, "SELECT COALESCE(balance, 0) FROM wallets WHERE user_id = %s", (db_user_id,))
                    new_balance = int(wallet_row[0]) if wallet_row else 0
                else:
                    cur.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (db_user_id,))
                    wallet_row = cur.fetchone()
                    if not wallet_row:
                        raise ValueError("Wallet not found for user")
                    wallet_balance = int(wallet_row[0])

                    final_price, base_gb, bonus_gb, max_gb = apply_promo_and_calculate_final_price(
                        cur, server_plan_price, db_user_id, int(traffic_gb)
                    )

                    cur.execute("SELECT COALESCE(discount_percentage, 0) FROM users WHERE id = %s", (db_user_id,))
                    discount_percentage = float(cur.fetchone()[0] or 0)

                    if wallet_balance < final_price:
                        raise ValueError("INSUFFICIENT_BALANCE: wallet balance is not enough")

                    new_balance = wallet_balance - final_price
                    cur.execute(
                        "UPDATE wallets SET balance = %s WHERE user_id = %s",
                        (new_balance, db_user_id)
                    )

                config_data = {
                    "uuid": client_uuid,
                    "server": server_name,
                    "address": server_address,
                    "protocol": protocol,
                    "port": port,
                    "inbound_id": inbound_id,
                    "remark": f"💠 {base_gb} گیگ ⏳ {duration_days} روزه 💳 {server_plan_price} تومان",
                    "connection_url": url,
                    "subscription_url": subscription_url,
                    "price": int(server_plan_price),
                    "expiry": expiry_date.strftime("%Y-%m-%d")
                }

                cur.execute("""
                    INSERT INTO purchased_plans
                        (user_id, plan_id, name, purchase_date, expiry_date,
                         used_traffic_gb, base_traffic_gb, bonus_traffic_gb, max_traffic_gb,
                         is_active, config_data)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                """, (
                    db_user_id,
                    plan_id,
                    purchased_plan_name,
                    purchase_date,
                    expiry_date,
                    0,
                    base_gb,
                    bonus_gb,
                    max_gb,
                    True,
                    json.dumps(config_data),
                ))
                purchased_plan_id = cur.fetchone()[0]

                description = f'خرید طرح: 💠 {max_gb} گیگ ⏳ {duration_days} روزه 💳 {final_price:,} تومان'
                if admin_user:
                    description += ' (ادمین - رایگان)'
                elif discount_percentage > 0:
                    description += f' (تخفیف {discount_percentage:g}%)'

                cur.execute("""
                    INSERT INTO wallet_transactions (
                        user_id, amount, type, method, description, related_plan_id, status, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    db_user_id,
                    final_price,
                    'spend',
                    'admin_free' if admin_user else None,
                    description,
                    purchased_plan_id,
                    'success',
                    purchase_date
                ))

                cur.execute(
                    "UPDATE servers SET current_users = current_users + 1 WHERE id = %s",
                    (server_id,)
                )

    except Exception as e:
        if str(e).startswith("INSUFFICIENT_BALANCE"):
            await update.message.reply_text("❌ موجودی کیف پول کافی نیست.")
        elif client_uuid:
            await update.message.reply_text(f"❌ خطا در خرید: {e}")
        else:
            await context.bot.send_message(
                ADMIN_ID,
                f"""
⚠️ <b>گزارش ساخت اکانت جدید (ناموفق)</b>
━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>کاربر:</b> @{user_username or 'نامشخص'}
🆔 <b>آیدی عددی:</b> <code>{telegram_user_id}</code>
🏷 <b>نام کانفیگ (کاربر):</b> <code>{purchased_plan_name}</code>

📦 <b>طرح انتخابی:</b> <code>💠 {traffic_gb} گیگ ⏳ {duration_days} روزه 💳 {server_plan_price}</code>
🌐 <b>سرور:</b> {server_name}
🆔 <b>Inbound ID:</b> {inbound_id}

📄 <b>وضعیت:</b> {escape(str(e))}
━━━━━━━━━━━━━━━━━━━━━━━
""",
                parse_mode="HTML"
            )
            await update.message.reply_text(
                "❌ دریافت اکانت با مشکل مواجه شد.\n\n💬 لطفاً با پشتیبانی در ارتباط باشید."
            )

        if client_uuid:
            try:
                await context.bot.send_message(
                    ADMIN_ID,
                    f"⚠️ خرید DB Fail شد ولی کلاینت ساخته شده.\n"
                    f"User: {telegram_user_id}\nConfigName: {purchased_plan_name}\n"
                    f"PlanId: {plan_id}\nServer: {server_name}\nUUID: {client_uuid}\nError: {e}"
                )
            except Exception:
                logger.exception("failed to notify admin after buy failure")
        return

    finally:
        pool.putconn(db_conn)

    try:
        email = unquote((url or "").split("#")[-1].strip()) if "#" in (url or "") else f"{client_uuid}@local"
        await update_panel_client(
            base_url=base_url,
            username=server_username,
            password=server_password,
            inbound_id=inbound_id,
            client_uuid=client_uuid,
            email=email,
            expiry_date=expiry_date,
            max_gb=max_gb,
            enable=True,
        )
    except Exception:
        logger.exception("post-buy panel update failed")

    await context.bot.send_message(
        ADMIN_ID,
        f"""
⚠️ <b>گزارش ساخت اکانت جدید</b>
━━━━━━━━━━━━━━━━━━━━━━━
👤 <b>کاربر:</b> @{user_username or 'نامشخص'}
🆔 <b>آیدی عددی:</b> <code>{telegram_user_id}</code>
🏷 <b>نام کانفیگ (کاربر):</b> <code>{purchased_plan_name}</code>

📦 <b>طرح نهایی:</b> <code>💠 {base_gb} گیگ ⏳ {duration_days} روزه 💳 {server_plan_price}</code>
🧮 <b>حجم نهایی:</b> {max_gb}GB
⏳ <b>مدت:</b> {duration_days} روز

🌐 <b>سرور:</b> {server_name}
🔌 <b>آدرس:</b> <code>{server_address}</code>
🆔 <b>Inbound ID:</b> {inbound_id}

📄 <b>وضعیت:</b> ✅ ثبت در دیتابیس و کیف پول موفق
━━━━━━━━━━━━━━━━━━━━━━━
""",
        parse_mode="HTML"
    )

    # -------------------------
    # Phase 4: UI responses (use max_gb)
    # -------------------------
    context.user_data.pop("plan_price", None)
    context.user_data.pop("wallet_balance", None)
    clear_timed_value(context, "awaiting_confirm_plan")
    context.user_data.pop("selected_plan_id", None)

    await send_config_with_qr(
        update,
        context,
        url=url,
        max_gb=max_gb,
        duration_days=duration_days,
        expiry_date=expiry_date,
        prefix_text="✅ خرید شما با موفقیت انجام شد!",
        subscription_url=subscription_url,
    )
    await show_buy_server_panel(update, context, message="⬅️ شما به منوی خرید سرور برگشتید")
    return


def get_next_reward(current_level: int):
    next_level = current_level + 1
    for r in REWARD_LADDER:
        if r["level"] == next_level:
            return r
    return None


def format_reward_ladder_text():
    lines = ["🏆 <b>جدول جوایز دعوت (پله‌ای)</b>"]
    for r in REWARD_LADDER:
        lines.append(f"• سطح {r['level']}: {r['invites']} دعوت → {r['title']}")
    return "\n".join(lines)


def get_db_user_and_invites(user_id: int):
    row = q_one("SELECT id, name, username, reward_level FROM users WHERE user_id = %s", (user_id,))
    if not row:
        return None

    db_user_id, name, username, reward_level = row

    invited_count = q_one("SELECT COUNT(*) FROM users WHERE refered_by = %s", (db_user_id,))[0]

    return db_user_id, name, username, (reward_level or 0), invited_count


async def handle_profile_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        text = update.message.text
    elif update.callback_query:
        text = update.callback_query.data
    else:
        text = ""
    user_id = update.effective_user.id
    db_user_id = q_one("""
    SELECT id from users where user_id = %s
    """, (user_id,))
    if text in {"🧪 بررسی سرور دلخواه", "profile_custom_server"}:
        await update.effective_message.reply_text(
            "🔍 <b>بررسی کانفیگ دلخواه</b>\n\n"
            "لطفاً فقط <b>کانفیگ مورد نظر</b>را ارسال کنید تا بررسی شود.",
            parse_mode="HTML"
        )
        context.user_data["awaiting_server_detail"] = True
        return
    if text in {"👤 نمایش اطلاعات کاربر", "profile_user_info"}:
        user_row = q_one("""
            SELECT 
                u.id, u.user_id, u.name, u.username, u.phone, 
                w.balance, u.discount_percentage, u.freetrial,
                u.refcode, u.refered_by
            FROM users u
            JOIN wallets w ON u.id = w.user_id
            WHERE u.user_id = %s
        """, (user_id,))

        (user_id_internal, user_telegram_id, user_name, user_username, user_phone,
         user_wallet, user_discount, user_freetrial, refcode, refered_by_id) = user_row
        if refered_by_id:
            referer_data = q_one("SELECT name, username FROM users WHERE user_id = %s", (refered_by_id,))
            if referer_data:
                referer_name, referer_username = referer_data
                referer_display = f"{referer_name or ''} (@{referer_username})" if referer_username else referer_name or "نامشخص"
            else:
                referer_display = "نامشخص"
        else:
            referer_display = "وارد نشده"
        total, active, inactive = q_one("""
            SELECT 
                COUNT(*) AS total,
                SUM(CASE WHEN is_active THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN NOT is_active THEN 1 ELSE 0 END) AS inactive
            FROM purchased_plans 
            WHERE user_id = %s
        """, (user_id_internal,))

        await update.effective_message.reply_text(
            f"👤 <b>اطلاعات کاربر:</b>\n"
            f"🆔 آیدی عددی: <code>{user_id}</code>\n"
            f"📛 نام: {user_name or 'نامشخص'}\n"
            f"🔗 یوزرنیم: @{user_username or 'ندارد'}\n"
            f"📞 شماره: {user_phone or 'ثبت نشده'}\n"
            f"💰 کیف پول: {user_wallet:,} تومان\n"
            f"🎁 تخفیف: {user_discount or 0}%\n"
            f"🧪 تست رایگان: {'استفاده کرده' if user_freetrial else 'استفاده نکرده'}\n"
            f"\n"
            f"🔗 <b>کد دعوت شما:</b> <code>{refcode or 'تولید نشده'}</code>\n"
            f"🙋‍♂️ <b>معرف شما:</b> {referer_display}\n"
            f"\n"
            f"📦 مجموع طرح‌ها: {total}\n"
            f"✅ فعال: {active} | ❌ غیرفعال: {inactive}",
            parse_mode="HTML"
        )
    if context.user_data.get("awaiting_server_detail"):
        try:
            try:
                main_address = text.split('@')[1].split(":")[0]
            except IndexError:
                await update.effective_message.reply_text("❗ فرمت کانفیگ اشتباه است. لطفاً دوباره ارسال کنید.")
                return
            result = q_one("""
                SELECT base_url, username, password, address
                FROM servers
                WHERE address = %s
                ORDER BY created_at ASC
            """, (main_address,))
            if not result:
                await update.effective_message.reply_text("❗ سرور مورد نظر در دیتابیس یافت نشد.")
                return
            base_url, username, password, address = result
            try:
                inbounds_data = await fetch_panel_inbounds(
                    base_url=base_url,
                    username=username,
                    password=password,
                )
            except Exception:
                await update.effective_message.reply_text("❌ ورود به پنل ناموفق بود.")
                return
            found = False
            UUID_id = text.split("@", 1)[0].rsplit("/", 1)[1]
            for inbound in inbounds_data.get("obj", []):
                port = inbound.get("port")
                protocol = inbound.get("protocol")

                settings = json.loads(inbound.get("settings", "{}"))
                clients = settings.get("clients", [])

                for client in clients:
                    if client.get("id") == UUID_id:
                        found = True
                        expiry_ts = client.get("expiryTime", 0)
                        if expiry_ts:
                            expiry_date = datetime.fromtimestamp(expiry_ts / 1000)
                            expiry = expiry_date.strftime('%Y-%m-%d %H:%M')
                            now = datetime.now()
                            remaining_days = (expiry_date - now).days
                            remaining = f"{remaining_days} روز باقی مانده" if remaining_days >= 0 else "⛔ منقضی شده"
                        else:
                            expiry = "∞ Unlimited"
                            remaining = "⏳ نامحدود"
                        total_gb = round(client.get("totalGB", 0) / (1024 ** 3), 2)
                        uuid = client.get("id")
                        traffic_resp = session.get(f"{base_url}/panel/api/inbounds/getClientTrafficsById/{uuid}")
                        traffic_data = traffic_resp.json()
                        if traffic_data.get("obj"):
                            traffic_obj = traffic_data["obj"][0]
                            up = traffic_obj.get("up", 0)
                            down = traffic_obj.get("down", 0)
                            used_gb = round((up + down) / (1024 ** 3), 2)
                        else:
                            used_gb = 0
                        config_url = (
                            f"{protocol}://{client.get('id')}@{address}:{port}"
                            f"?type=tcp&path=%2F&headerType=http&security=none#{client.get('email')}"
                        )
                        await send_qr_code(update, context, config_url)
                        await update.effective_message.reply_text(
                            f"✅ <b>کاربر یافت شد</b>\n\n"
                            f"📧 <b>Email:</b> <code>{client.get('email')}</code>\n"
                            f"📆 <b>انقضا:</b> <code>{expiry}</code>\n"
                            f"⏳ <b>مانده:</b> <code>{remaining}</code>\n"
                            f"📊 <b>حجم:</b> <code>{total_gb} GB</code>\n"
                            f"📥 <b>مصرف‌شده:</b> <code>{used_gb} GB</code>\n"
                            f"🔗 <b>لینک:</b>\n<code>{config_url}</code>",
                            parse_mode="HTML"
                        )
                        context.user_data.pop("awaiting_server_detail", None)
                        break
                if found:
                    break
            if not found:
                await update.effective_message.reply_text("❗ کاربر با UUID داده شده یافت نشد.")
        except Exception as e:
            print(f"❌ Exception: {e}")
            await update.effective_message.reply_text("⚠️ خطایی رخ داد. لطفاً بعداً دوباره تلاش کنید.")
    if text in {"📁 کانفیگ‌های من", "return_user_purchased"}:
        await handle_user_purchased_plans(update, context)
    if text in {"👥 لینک دعوت من", "profile_referral"}:
        result = q_one("SELECT refcode FROM users WHERE user_id = %s", (user_id,))
        if not result:
            await update.effective_message.reply_text("❗ حساب کاربری شما یافت نشد.")
            return

        refcode = result[0] or f"ref_{user_id}"

        invite_link = f"https://t.me/mrfoxvpn_bot?start={refcode}"

        row = q_one("SELECT id FROM users WHERE user_id = %s", (user_id,))
        if not row:
            await update.effective_message.reply_text("❗ حساب کاربری شما یافت نشد.")
            return
        db_user_id = row[0]

        invited_count = q_one("SELECT COUNT(*) FROM users WHERE refered_by = %s", (db_user_id,))[0]

        await update.effective_message.reply_text(
            f"👥 <b>لینک دعوت شما:</b>\n"
            f"🔗 <code>{invite_link}</code>\n\n"
            f"👤 تعداد افرادی که با لینک شما ثبت‌نام کرده‌اند: <b>{invited_count}</b>\n"
            f"\n"
            f"📢 با ارسال این لینک به دوستان‌تان، پس از ثبت‌نام آن‌ها می‌توانید پاداش دریافت کنید.",
            parse_mode="HTML"
        )
    if text in {"🎁 درخواست جایزه", "profile_reward"}:
        data = get_db_user_and_invites(user_id)
        if not data:
            await update.effective_message.reply_text("❗ حساب کاربری شما یافت نشد.")
            return

        db_user_id, name, username, reward_level, invited_count = data
        username_str = f"@{username}" if username else "بدون یوزرنیم"

        next_reward = get_next_reward(reward_level)

        if not next_reward:
            await update.effective_message.reply_text(
                "🎉 شما تمام جوایز پله‌ای را دریافت کرده‌اید.\n\n"
                "اگر کمپین جدیدی فعال شود اطلاع‌رسانی می‌کنیم.",
                parse_mode="HTML"
            )
            return

        need_invites = next_reward["invites"]
        reward_title = next_reward["title"]
        remaining = max(0, need_invites - invited_count)

        if invited_count < need_invites:
            await update.effective_message.reply_text(
                f"🎁 <b>وضعیت جوایز شما</b>\n\n"
                f"👥 دعوت موفق: <b>{invited_count}</b>\n"
                f"📌 سطح فعلی: <b>{reward_level}</b>\n"
                f"🎯 جایزه بعدی (سطح {next_reward['level']}): <b>{reward_title}</b>\n"
                f"⏳ باقی‌مانده تا جایزه: <b>{remaining}</b> دعوت\n\n"
                f"{format_reward_ladder_text()}",
                parse_mode="HTML"
            )
            return

        already_pending = q_one(
            "SELECT 1 FROM reward_requests WHERE user_id = %s AND level = %s AND status = 'pending' LIMIT 1",
            (db_user_id, next_reward["level"])
        ) is not None
        if already_pending:
            await update.effective_message.reply_text(
                "⏳ درخواست شما قبلاً ثبت شده و در حال بررسی است.\n"
                "لطفاً کمی بعد دوباره چک کنید.",
                parse_mode="HTML"
            )
            return

        req_id = q_one(
            "INSERT INTO reward_requests (user_id, level, invited_count, status, created_at) VALUES (%s, %s, %s, 'pending', NOW()) RETURNING id",
            (db_user_id, next_reward["level"], invited_count)
        )[0]

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید و ثبت شد", callback_data=f"reward_approve_{req_id}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reward_reject_{req_id}")
            ]
        ])

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"🎁 <b>درخواست جایزه جدید</b>\n\n"
                f"👤 کاربر: {name} ({username_str})\n"
                f"🆔 Telegram ID: <code>{user_id}</code>\n"
                f"🧾 DB User ID: <code>{db_user_id}</code>\n\n"
                f"👥 دعوت موفق: <b>{invited_count}</b>\n"
                f"🏅 سطح درخواستی: <b>{next_reward['level']}</b>\n"
                f"🎁 جایزه: <b>{reward_title}</b>\n\n"
                f"📌 بعد از اینکه هدیه را دستی دادید، روی «تایید» بزنید تا سطح کاربر ثبت شود."
            ),
            parse_mode="HTML",
            reply_markup=keyboard
        )

        await update.effective_message.reply_text(
            "✅ درخواست شما ثبت شد و برای ادمین ارسال گردید.\n"
            "پس از بررسی، نتیجه به شما اطلاع داده می‌شود.",
            parse_mode="HTML"
        )


async def handle_reward_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_reward_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    action = callback.get("action")
    req_id = callback.get("id")

    row = q_one(
        "SELECT id, user_id, level, status FROM reward_requests WHERE id = %s",
        (req_id,)
    )
    if not row:
        await query.edit_message_text("❗ درخواست یافت نشد.")
        return

    _, db_user_id, level, status = row
    if status != "pending":
        await query.edit_message_text("⚠️ این درخواست قبلاً بررسی شده است.")
        return

    urow = q_one("SELECT user_id, name, username, reward_level FROM users WHERE id = %s", (db_user_id,))
    if not urow:
        await query.edit_message_text("❗ کاربر یافت نشد.")
        return
    telegram_id, name, username, reward_level = urow
    username_str = f"@{username}" if username else "بدون یوزرنیم"

    if action == "approve":
        if (reward_level or 0) >= level:
            q_exec("UPDATE reward_requests SET status='approved', decided_at=NOW() WHERE id=%s", (req_id,))
            await query.edit_message_text("✅ ثبت شد (کاربر قبلاً این سطح را گرفته بود).")
            return

        q_exec("UPDATE users SET reward_level = %s WHERE id = %s", (level, db_user_id))
        q_exec("UPDATE reward_requests SET status='approved', decided_at=NOW() WHERE id=%s", (req_id,))

        reward_title = next((r["title"] for r in REWARD_LADDER if r["level"] == level), f"سطح {level}")

        await context.bot.send_message(
            chat_id=telegram_id,
            text=(
                f"🎉 <b>درخواست جایزه شما تایید شد</b>\n\n"
                f"🏅 سطح: <b>{level}</b>\n"
                f"🎁 جایزه: <b>{reward_title}</b>\n\n"
                f"✅ هدیه برای شما فعال/ارسال شد.\n"
                f"اگر سوالی دارید از بخش پشتیبانی پیام دهید."
            ),
            parse_mode="HTML"
        )

        await query.edit_message_text(
            f"✅ تایید شد و سطح کاربر ثبت شد.\n\n"
            f"👤 {name} ({username_str})\n"
            f"🏅 سطح جدید: {level}"
        )

    elif action == "reject":
        q_exec("UPDATE reward_requests SET status='rejected', decided_at=NOW() WHERE id=%s", (req_id,))

        await context.bot.send_message(
            chat_id=telegram_id,
            text="❌ درخواست جایزه شما رد شد.\nاگر فکر می‌کنید اشتباه شده، از پشتیبانی پیام دهید."
        )

        await query.edit_message_text("❌ درخواست رد شد و به کاربر اطلاع داده شد.")


MY_PLANS_PAGE_KEY = "my_plans_page"


def plan_emoji(is_active: bool) -> str:
    return "🟢" if is_active else "🔴"


async def handle_user_purchased_plans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data[MY_PLANS_PAGE_KEY] = 1

    row = q_one("SELECT id FROM users WHERE user_id = %s", (user_id,))
    if not row:
        await update.effective_message.reply_text("❌ کاربر یافت نشد.")
        return
    db_user_id = row[0]

    page = 1
    offset = 0

    plans = q_all("""
        SELECT id, name, is_active
        FROM purchased_plans
        WHERE user_id = %s
        ORDER BY purchase_date DESC
        LIMIT %s OFFSET %s
    """, (db_user_id, PAGE_SIZE + 1, offset))

    has_next = len(plans) > PAGE_SIZE
    plans = plans[:PAGE_SIZE]

    buttons = [
        [InlineKeyboardButton(f"{plan_emoji(bool(is_active))} {name or 'بدون نام'}",
                              callback_data=f"user_purchased_plan_{pid}")]
        for pid, name, is_active in plans
    ]

    nav = []
    if has_next:
        nav.append(InlineKeyboardButton("➡️ بعدی", callback_data="my_plans_next"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="return_my_profile")])

    text = "<b>📁 کانفیگ‌های شما:</b>\n(برای مشاهده جزئیات، یکی را انتخاب کنید)"

    await update.effective_message.reply_text(
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def handle_my_plans_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    page = context.user_data.get(MY_PLANS_PAGE_KEY, 1)
    if q.data == "my_plans_next":
        page += 1
    elif q.data == "my_plans_prev" and page > 1:
        page -= 1

    context.user_data[MY_PLANS_PAGE_KEY] = page
    offset = (page - 1) * PAGE_SIZE

    row = q_one("SELECT id FROM users WHERE user_id = %s", (update.effective_user.id,))
    if not row:
        return
    db_user_id = row[0]

    plans = q_all("""
        SELECT id, name, is_active
        FROM purchased_plans
        WHERE user_id = %s
        ORDER BY purchase_date DESC
        LIMIT %s OFFSET %s
    """, (db_user_id, PAGE_SIZE + 1, offset))

    has_next = len(plans) > PAGE_SIZE
    plans = plans[:PAGE_SIZE]

    buttons = [
        [InlineKeyboardButton(f"{plan_emoji(bool(is_active))} {name or 'بدون نام'}",
                              callback_data=f"user_purchased_plan_{pid}")]
        for pid, name, is_active in plans
    ]

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data="my_plans_prev"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ بعدی", callback_data="my_plans_next"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="return_my_profile")])

    text = "<b>📁 کانفیگ‌های شما:</b>\n(برای مشاهده جزئیات، یکی را انتخاب کنید)"

    await q.edit_message_text(
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_detail_purchased_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                              purchased_plan_id: int = None):
    logger.info("show_detail_purchased_plan_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    if purchased_plan_id is None:
        callback = parse_callback(query.data)
        purchased_plan_id = callback["id"]
    else:
        purchased_plan_id = int(purchased_plan_id)

    # --- DB (بدون cursor global) ---
    row = q_one("""
        SELECT
            name,
            purchase_date,
            expiry_date,
            used_traffic_gb,
            is_active,
            config_data,
            max_traffic_gb,
            deactivated_reason,
            deactivated_at
        FROM purchased_plans
        WHERE id = %s
    """, (purchased_plan_id,))
    if not row:
        await query.edit_message_text("❌ طرح مورد نظر یافت نشد.")
        return

    (name, purchase_date, expiry_date, used_db_gb, is_active,
     config_data_json, max_traffic_gb, deact_reason, deact_at) = row

    # --- parse config_data (همون قبلی) ---
    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        uuid = config_data.get("uuid")
        address = config_data.get("address")
        remark = config_data.get("remark")
        url = config_data.get("connection_url")
        subscription_url = config_data.get("subscription_url")
    except Exception as e:
        await query.edit_message_text(f"❌ خطا در خواندن کانفیگ: {e}")
        return

    # --- پنل (همون منطق قبلی) ---
    used_gb = up_gb = down_gb = remaining_gb = 0
    if uuid and address:
        srv = q_one("SELECT base_url, username, password FROM servers WHERE address = %s", (address,))
        if not srv:
            await query.edit_message_text("❌ سرور در دیتابیس یافت نشد.")
            return

        base_url, username, password = srv
        try:
            traffic_data = await fetch_panel_client_traffic(
                base_url=base_url,
                username=username,
                password=password,
                client_uuid=uuid,
            )
        except Exception:
            await query.edit_message_text("❌ ورود به پنل ناموفق بود.")
            return

        if not subscription_url:
            try:
                subscription_url = await get_sanaei_subscription_link(
                    base_url=base_url,
                    username=username,
                    password=password,
                    client_uuid=uuid,
                )
                if subscription_url:
                    config_data["subscription_url"] = subscription_url
                    q_exec("UPDATE purchased_plans SET config_data = %s WHERE id = %s", (json.dumps(config_data), purchased_plan_id))
            except Exception:
                logger.exception("failed to fetch purchased plan subscription link")

        if traffic_data.get("obj"):
            traffic_obj = traffic_data["obj"][0]
            up = traffic_obj.get("up", 0)
            down = traffic_obj.get("down", 0)
            used_gb = round((up + down) / (1024 ** 3), 2)
            up_gb = round(up / (1024 ** 3), 2)
            down_gb = round(down / (1024 ** 3), 2)

    # --- max/remaining (واضح‌تر) ---
    max_gb = float(max_traffic_gb or 0)
    used_show = used_gb if used_gb else float(used_db_gb or 0)
    remaining_gb = round(max(0, max_gb - used_show), 2) if max_gb > 0 else 0

    # --- آپدیت DB used_traffic_gb (بدون cursor/commit) ---
    q_exec("UPDATE purchased_plans SET used_traffic_gb = %s WHERE id = %s", (used_show, purchased_plan_id))

    # --- status + reason (DB فقط) ---
    status = "🟢 فعال" if is_active else "🔴 غیرفعال"

    reason_block = ""
    if not is_active:
        if deact_reason:
            reason_block = f"\n❌ <b>دلیل:</b> <code>{deact_reason}</code>"
        else:
            reason_block = "\nℹ️ <b>دلیل:</b> نامشخص (کانفیگ قدیمی)\n<b>اگر نیاز دارید با پشتیبانی تماس بگیرید.</b>"

    exp_txt = expiry_date.strftime("%Y-%m-%d %H:%M") if expiry_date else "—"
    pur_txt = purchase_date.strftime("%Y-%m-%d %H:%M") if purchase_date else "—"
    subscription_block = f"✨ <b>لینک اشتراک:</b>\n<code>{subscription_url}</code>\n" if subscription_url else ""

    text = (
        "<b>📡 اطلاعات سرویس</b>\n"
        f"وضعیت سرویس: {status}{reason_block}\n"
        f"🌻 <b>نام سرویس:</b> <code>{name}</code>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🖥 اطلاعات اتصال</b>\n"
        f"🔗 <b>لینک اتصال:</b>\n<code>{url}</code>\n"
        f"{subscription_block}"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>🔖 جزئیات طرح</b>\n"
        f"📦 <b>نام پلن:</b> {remark}\n"
        f"📅 <b>تاریخ خرید:</b> <code>{pur_txt}</code>\n"
        f"📆 <b>تاریخ انقضا:</b> <code>{exp_txt}</code>\n"
        f"📊 <b>مصرف:</b> <code>{used_show:.2f} / {max_gb:.2f} GB</code>\n"
        f"📉 <b>حجم باقی‌مانده:</b> <code>{remaining_gb:.2f} GB</code>\n"
        f"📤 <b>آپلود:</b> <code>{up_gb:.2f} GB</code>\n"
        f"📥 <b>دانلود:</b> <code>{down_gb:.2f} GB</code>\n"
    )

    context.user_data['url'] = url

    # ✅ کیبورد همون قبلی، دست‌نخورده
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton('📩 دریافت QR', callback_data=f"qr_code_purchased_plan"),
         InlineKeyboardButton('🏷 تغییر لینک اتصال', callback_data=f"change_link_purchased_plan_{purchased_plan_id}")],
        [InlineKeyboardButton('📝 تغییر اسم', callback_data=f"change_name_purchased_plan_{purchased_plan_id}"),
         InlineKeyboardButton('⚠️ غییر فعال/فعال سازی کانفیگ',
                              callback_data=f"dis_able_purchased_plan_{purchased_plan_id}")],
        [InlineKeyboardButton('❌ حذف کانفیگ', callback_data=f"delete_purchased_plan_{purchased_plan_id}"),
         InlineKeyboardButton('🔄 تمدید پلن فعلی', callback_data=f"renewal_purchased_plan_{purchased_plan_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_user_purchased")]
    ])

    context.user_data["purchased_old_name"] = name

    try:
        await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=keyboard)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def handle_user_renewal_purchased_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_user_renewal_purchased_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    purchased_plan_id = callback["id"]
    result = q_one("""
        SELECT 
            p.user_id,
            p.max_traffic_gb, 
            p.expiry_date, 
            p.config_data, 
            s.id, 
            s.base_url, 
            s.username, 
            s.password,
            sp.id AS server_plan_id,
            sp.category_id,
            sp.price,
            sp.traffic_gb,
            sp.duration_days
        FROM purchased_plans p
        LEFT JOIN server_plans sp ON p.plan_id = sp.id
        LEFT JOIN servers s ON sp.server_id = s.id
        WHERE p.id = %s
    """, (purchased_plan_id,))
    if not result:
        await query.message.edit_text("❌ اطلاعات سرور مربوطه یافت نشد.")
        return
    (
        user_id, max_traffic_gb, expiry_date, config_data_json,
        server_id, base_url, username, password,
        server_plan_id, category_id,
        plan_price, plan_traffic_gb, plan_duration_days
    ) = result
    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        client_uuid = config_data.get("uuid")
        inbound_id = config_data.get("inbound_id")
    except Exception as e:
        await query.message.edit_text(f"❌ خطا در خواندن کانفیگ: {e}")
        return
    try:
        used_gb = 0
        traffic_data = await fetch_panel_client_traffic(
            base_url=base_url,
            username=username,
            password=password,
            client_uuid=client_uuid,
        )
        traffic_obj = traffic_data.get("obj", [])[0]
        used_gb = round((traffic_obj.get("up", 0) + traffic_obj.get("down", 0)) / (1024 ** 3), 2)
    except Exception as e:
        print(f"❌ Failed to parse traffic for plan {purchased_plan_id}: {e}")

    percent_used = (used_gb / max_traffic_gb) * 100
    if isinstance(expiry_date, str):
        expiry_date = datetime.fromisoformat(expiry_date)

    days_left = (expiry_date - datetime.now()).days
    if percent_used < 80 and days_left >= 10:
        msg = "⚠️ <b>شرایط تمدید احراز نشد</b>\n\n"

        if percent_used < 80:
            msg += "📉 <u>حجم مصرفی کمتر از ۸۰٪ است</u>\n"
        if days_left >= 10:
            msg += "📅 <u>بیش از ۱۰ روز تا پایان اعتبار باقی مانده است</u>\n"

        msg += (
            "\nبرای تمدید، باید حداقل یکی از شروط زیر برقرار باشد:\n"
            "✅ <b>مصرف حداقل ۸۰٪ از حجم پلن</b>\n"
            "✅ <b>یا کمتر از ۱۰ روز تا پایان اعتبار</b>"
        )

        await query.edit_message_text(text=msg, parse_mode="HTML")
        await show_profile_panel(update, context)
        return
    elif not server_plan_id or not category_id:
        await query.edit_message_text(
            text=(
                "❌ <b>پلن مورد نظر در حال حاضر معتبر نیست</b>\n"
                "ممکن است این پلن یا دسته‌بندی آن حذف شده باشد.\n\n"
                "لطفاً یک پلن جدید انتخاب کنید یا با پشتیبانی تماس بگیرید."
            ),
            parse_mode="HTML"
        )
        await show_profile_panel(update, context)
        return
    wallet_row = q_one("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    wallet_balance = wallet_row[0] if wallet_row else 0
    user_row = q_one("SELECT discount_percentage FROM users WHERE id = %s", (user_id,))
    if not user_row:
        await query.message.edit_text("❌ کاربر یافت نشد.")
        return
    discount_percentage = float(user_row[0] or 0)
    discount_percentage = float(discount_percentage or 0)
    previous_plan_remark = config_data['remark']
    current_plan_remark = f"💠 {plan_traffic_gb} گیگ ⏳ {plan_duration_days} روزه 💳 {plan_price} تومان"
    final_price = plan_price
    if discount_percentage > 0:
        final_price = int(plan_price * (1 - discount_percentage / 100))
    context.user_data["cancel_renewal_plan_id"] = purchased_plan_id
    context.user_data['current_plan'] = current_plan_remark
    if previous_plan_remark != current_plan_remark:
        renewal_text = (
            "⚠️ <b>تفاوتی بین طرح قبلی و فعلی وجود دارد</b>\n\n"
            f"🔁 <b>طرح قبلی:</b>\n<code>{previous_plan_remark}</code>\n\n"
            f"🆕 <b>طرح فعلی:</b>\n<code>{current_plan_remark}</code>\n\n"
            f"💳 <b>قیمت:</b> {final_price:,} تومان\n"
            f"💰 <b>موجودی کیف پول:</b> {wallet_balance:,} تومان\n\n"
        )
        if discount_percentage > 0:
            renewal_text += f"🎁 <b>تخفیف شما:</b> {discount_percentage:.0f}%\n"
        if wallet_balance >= final_price:
            renewal_text += "آیا مایل به تمدید این طرح هستید؟"
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ تمدید کن", callback_data=f"confirm_renewal_{purchased_plan_id}"),
                    InlineKeyboardButton("❌ انصراف", callback_data="cancel_renewal")
                ]
            ])
        else:
            renewal_text += (
                "❌ <b>موجودی کیف پول شما کافی نیست.</b>\n"
                "💸 لطفاً ابتدا کیف پول خود را شارژ کنید."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="go_to_wallet")]
            ])

        await query.edit_message_text(
            text=renewal_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return
    elif previous_plan_remark == current_plan_remark:
        renewal_text = (
            "🔄 <b>طرح فعلی شما آماده تمدید است</b>\n\n"
            f"📦 <b>طرح:</b>\n<code>{current_plan_remark}</code>\n\n"
            f"💳 <b>قیمت:</b> {final_price:,} تومان\n"
            f"💰 <b>موجودی کیف پول:</b> {wallet_balance:,} تومان\n\n"
        )
        if discount_percentage > 0:
            renewal_text += f"🎁 <b>تخفیف شما:</b> {discount_percentage:.0f}%\n"
        if wallet_balance >= final_price:
            renewal_text += "آیا مایل به تمدید این طرح هستید؟"
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("✅ تمدید کن", callback_data=f"confirm_renewal_{purchased_plan_id}"),
                    InlineKeyboardButton("❌ انصراف", callback_data="cancel_renewal")
                ]
            ])
        else:
            renewal_text += (
                "❌ <b>موجودی کیف پول شما کافی نیست.</b>\n"
                "💸 لطفاً ابتدا کیف پول خود را شارژ کنید."
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 شارژ کیف پول", callback_data="go_to_wallet"),
                 InlineKeyboardButton("❌ انصراف", callback_data="cancel_renewal")]
            ])

        await query.edit_message_text(
            text=renewal_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        return


async def handle_confirm_renewal_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("handle_confirm_renewal_plan_callback tg_user=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    purchased_plan_id = callback["id"]
    current_plan_remark = context.user_data.get('current_plan')
    if not current_plan_remark:
        await query.edit_message_text("❌ اطلاعات پلن برای تمدید یافت نشد.")
        return

    # پارس اطلاعات از متن (مثل کد قبلی خودت)
    current_plan = current_plan_remark.split()
    try:
        price = int(current_plan[7])
        days = int(current_plan[4])
        traffic = int(current_plan[1])  # این همون base هست
    except Exception:
        await query.edit_message_text("❌ فرمت اطلاعات پلن نامعتبر است.")
        return

    user = query.from_user
    tg_username = user.username
    tg_user_id = user.id

    now_dt = datetime.now()
    new_expiry = now_dt + timedelta(days=days)

    # -------------------------
    # Phase 1: DB (atomic) - promo + wallet + update purchased_plan + wallet tx
    # -------------------------
    final_price = price
    new_balance = 0
    discount_percentage = 0.0
    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        p.user_id,
                        p.name,
                        p.config_data,
                        s.id,
                        s.base_url, s.username, s.password
                    FROM purchased_plans p
                    LEFT JOIN server_plans sp ON p.plan_id = sp.id
                    LEFT JOIN servers s ON sp.server_id = s.id
                    WHERE p.id = %s
                """, (purchased_plan_id,))
                row = cur.fetchone()
                if not row:
                    await query.edit_message_text("❌ اطلاعات سرور یافت نشد.")
                    return

                db_user_id, purchased_plan_name, config_data_json, server_id, base_url, server_username, server_password = row

                config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else (config_data_json or {})
                client_uuid = config_data.get("uuid")
                inbound_id = config_data.get("inbound_id")
                if not client_uuid or not inbound_id:
                    await query.edit_message_text("❌ اطلاعات کانفیگ ناقص است.")
                    return

                cur.execute("SELECT balance FROM wallets WHERE user_id = %s FOR UPDATE", (db_user_id,))
                wallet_row = cur.fetchone()
                if not wallet_row:
                    raise ValueError("Wallet not found for user")

                current_balance = int(wallet_row[0])
                final_price, base_gb, bonus_gb, max_gb = apply_promo_and_calculate_final_price(
                    cur, price, db_user_id, int(traffic)
                )

                cur.execute("SELECT COALESCE(discount_percentage, 0) FROM users WHERE id = %s", (db_user_id,))
                discount_percentage = float(cur.fetchone()[0] or 0)
                if current_balance < final_price:
                    raise ValueError("INSUFFICIENT_BALANCE: wallet balance is not enough")

                new_balance = current_balance - final_price
                cur.execute(
                    "UPDATE wallets SET balance = %s, updated_at = NOW() WHERE user_id = %s",
                    (new_balance, db_user_id)
                )

                config_data["price"] = final_price
                config_data["remark"] = f"💠 {base_gb} گیگ ⏳ {days} روزه 💳 {price} تومان"

                cur.execute("""
                    UPDATE purchased_plans
                    SET expiry_date      = %s,
                        base_traffic_gb  = %s,
                        bonus_traffic_gb = %s,
                        max_traffic_gb   = %s,
                        used_traffic_gb  = 0,
                        is_active        = TRUE,
                        config_data      = %s,
                        purchase_date    = NOW()
                    WHERE id = %s
                    RETURNING expiry_date, purchase_date
                """, (
                    new_expiry,
                    base_gb,
                    bonus_gb,
                    max_gb,
                    json.dumps(config_data),
                    purchased_plan_id
                ))
                updated_expiry, updated_purchase_date = cur.fetchone()

                description = f"{config_data['remark']} ***renewal"
                cur.execute("""
                    INSERT INTO wallet_transactions (
                        user_id, amount, type, method, description, related_plan_id, status, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                """, (
                    db_user_id,
                    final_price,
                    'spend',
                    None,
                    description,
                    purchased_plan_id,
                    'success'
                ))

                cur.execute(
                    "UPDATE servers SET current_users = current_users WHERE id = %s",
                    (server_id,)
                )

    except Exception as e:
        if str(e).startswith("INSUFFICIENT_BALANCE"):
            await query.edit_message_text("❌ موجودی کیف پول کافی نیست.")
            return
        await query.edit_message_text(f"❌ خطا در تمدید: {e}")
        return
    finally:
        pool.putconn(db_conn)

    # -------------------------
    # Phase 2: Panel update (network) - ✅ uses max_gb
    # -------------------------
    raw_url = config_data.get("connection_url", "")
    email = unquote(raw_url.split("#")[-1].strip()) if "#" in raw_url else f"{client_uuid}@local"
    subscription_url = config_data.get("subscription_url")

    try:
        reset_resp, update_resp = await update_panel_client(
            base_url=base_url,
            username=server_username,
            password=server_password,
            inbound_id=inbound_id,
            client_uuid=client_uuid,
            email=email,
            expiry_date=updated_expiry,
            max_gb=max_gb,
            enable=True,
        )
    except RuntimeError:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "❌ <b>Renewal: ورود به سرور ناموفق بود (DB آپدیت شده)</b>\n"
                f"👤 @{tg_username or 'نامشخص'} | <code>{tg_user_id}</code>\n"
                f"🏷 <b>نام کانفیگ:</b> <code>{purchased_plan_name}</code>\n"
                f"📦 <b>حجم نهایی:</b> {max_gb}GB\n"
                f"🧾 PlanID: <code>{purchased_plan_id}</code>\n"
            ),
            parse_mode="HTML"
        )
        await query.edit_message_text("❌ ورود به سرور ناموفق بود.")
        return
    try:
        subscription_url = await get_sanaei_subscription_link(
            base_url=base_url,
            username=server_username,
            password=server_password,
            client_uuid=client_uuid,
        )
        if subscription_url:
            config_data["subscription_url"] = subscription_url
            q_exec("UPDATE purchased_plans SET config_data = %s WHERE id = %s", (json.dumps(config_data), purchased_plan_id))
    except Exception:
        logger.exception("failed to fetch renewal subscription link")
    if not update_resp.ok or not update_resp.json().get("success"):
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                "❌ <b>Renewal: بروزرسانی پنل ناموفق بود (DB آپدیت شده)</b>\n"
                f"👤 @{tg_username or 'نامشخص'} | <code>{tg_user_id}</code>\n"
                f"🏷 <b>نام کانفیگ:</b> <code>{purchased_plan_name}</code>\n"
                f"📦 <b>حجم نهایی:</b> {max_gb}GB\n"
                f"🧾 PlanID: <code>{purchased_plan_id}</code>\n"
                f"📄 پاسخ:\n<code>{escape(update_resp.text)}</code>"
            ),
            parse_mode="HTML"
        )
        await query.edit_message_text("❌ بروزرسانی سرور با خطا مواجه شد.")
        return

    # -------------------------
    # Phase 3: user message (✅ max_gb)
    # -------------------------
    renewal_subscription_block = f"✨ <b>لینک اشتراک:</b>\n<code>{subscription_url}</code>" if subscription_url else ""
    await query.message.reply_text(
        "✅ تمدید پلن با موفقیت انجام شد.\n"
        f"🕒 <b>تاریخ خرید:</b> {updated_purchase_date.strftime('%Y-%m-%d %H:%M')}\n"
        f"📅 <b>تاریخ جدید انقضا:</b> {updated_expiry.strftime('%Y-%m-%d %H:%M')}\n"
        f"📦 <b>حجم جدید:</b> {max_gb} GB\n"
        f"💳 <b>مبلغ کسر شده:</b> {final_price:,} تومان\n"
        f"💰 <b>موجودی جدید:</b> {new_balance:,} تومان\n"
        f"{renewal_subscription_block}",
        parse_mode="HTML"
    )

    context.user_data.pop("current_plan", None)
    context.user_data.pop("cancel_renewal_plan_id", None)

    await show_detail_purchased_plan_callback(update, context, purchased_plan_id)


async def handle_cancel_renewal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    purchased_plan_id = context.user_data.get("cancel_renewal_plan_id")
    if not purchased_plan_id:
        await query.edit_message_text("❗ شناسه پلن برای بازگشت یافت نشد.")
        return
    await show_detail_purchased_plan_callback(update, context, purchased_plan_id)


async def handle_user_delete_purchased_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    purchased_plan_id = query.data.rsplit('_', 1)[1]
    context.user_data['selected_purchased_id'] = purchased_plan_id
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ بله، حذف شود", callback_data="confirm_delete_purchased_plan"),
            InlineKeyboardButton("❌ خیر، انصراف", callback_data="cancel_delete_purchased_plan")
        ]
    ])
    await query.message.edit_text(
        "⚠️ <b>آیا مطمئن هستید که می‌خواهید این اشتراک را حذف کنید؟</b>\n"
        "⛔ بعد از حذف، امکان بازگشت وجود ندارد.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_user_confirm_delete_purchased_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    purchased_plan_id = context.user_data.get("selected_purchased_id")
    if not purchased_plan_id:
        await query.message.edit_text("❌ مشکلی در حذف پیش آمد.")
        return

    result = q_one("""
        SELECT p.config_data, s.id, s.base_url, s.username, s.password
        FROM purchased_plans p
        JOIN server_plans sp ON p.plan_id = sp.id
        JOIN servers s ON sp.server_id = s.id
        WHERE p.id = %s
    """, (purchased_plan_id,))

    if not result:
        await query.message.edit_text("❌ اطلاعات سرور مربوطه یافت نشد.")
        return

    config_data_json, server_id, base_url, username, password = result

    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        uuid = config_data.get("uuid")
        inbound_id = config_data.get("inbound_id")
    except Exception as e:
        await query.message.edit_text(f"❌ خطا در خواندن کانفیگ: {e}")
        return

    try:
        await delete_panel_client(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id,
            client_uuid=uuid,
        )
    except Exception as e:
        await query.message.edit_text(f"⚠️ اشتراک حذف نشد: {e}")
        return

    q_exec("DELETE FROM purchased_plans WHERE id = %s", (purchased_plan_id,))
    q_exec("""
        UPDATE servers
        SET current_users = current_users - 1
        WHERE id = %s
    """, (server_id,))

    context.user_data.pop("selected_purchased_id", None)
    context.user_data.pop("awaiting_user_delete_purchased", None)

    await query.message.edit_text("✅ اشتراک با موفقیت از دیتابیس و سرور حذف شد.")
    await show_profile_panel(update, context)


async def handle_cancel_delete_purchased_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.edit_text("❌ حذف لغو شد.")
    purchased_plan_id = context.user_data.get("selected_purchased_id")
    await show_detail_purchased_plan_callback(update, context, purchased_plan_id)
    context.user_data.pop("awaiting_user_delete_purchased", None)
    context.user_data.pop("selected_purchased_id", None)


async def handle_dis_able_purchased_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    purchased_plan_id = callback["id"]
    result = q_one("""
            SELECT config_data, is_active FROM purchased_plans WHERE id = %s
        """, (purchased_plan_id,))
    config_data_json, is_active = result
    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        client_uuid = config_data.get("uuid")
        address = config_data.get("address")
        inbound_id = config_data.get("inbound_id")
        protocol = config_data.get("protocol")
        port = config_data.get("port")
    except Exception as e:
        return
    result = q_one("SELECT base_url, username, password FROM servers WHERE address = %s", (address,))
    if not result:
        await query.edit_message_text("❌ اطلاعات سرور یافت نشد.")
        return
    base_url, username, password = result
    try:
        inbound_list = await fetch_panel_inbounds(
            base_url=base_url,
            username=username,
            password=password,
        )
    except Exception:
        await query.edit_message_text("❌ ورود به پنل ناموفق بود.")
        return
    found_client = find_panel_client(inbound_list, inbound_id, client_uuid)
    if not found_client:
        await query.edit_message_text("❌ کلاینت مورد نظر در پنل یافت نشد.")
        return
    new_enable_status = not found_client.get('enable', True)
    found_client['enable'] = new_enable_status
    try:
        await update_panel_client_payload(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id,
            client_uuid=client_uuid,
            client_payload=found_client,
        )
    except Exception:
        await query.edit_message_text("❌ تغییر وضعیت با خطا مواجه شد.")
        return
    config_data['enable'] = new_enable_status
    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE purchased_plans
                    SET config_data = %s, is_active = %s
                    WHERE id = %s
                """, (
                    json.dumps(config_data),
                    new_enable_status,
                    purchased_plan_id
                ))
    finally:
        pool.putconn(db_conn)
    status_text = "🟢 فعال شد" if new_enable_status else "⛔ غیرفعال شد"
    await query.edit_message_text(
        f"✅ وضعیت کانفیگ با موفقیت تغییر یافت.\n\n"
        f"<b>وضعیت جدید:</b> <code>{status_text}</code>",
        parse_mode="HTML"
    )
    await show_profile_panel(update, context)


async def handle_change_name_purchased_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    purchased_plan_id = callback_data.rsplit('_', 1)[1]
    context.user_data['awaiting_change_name_purchased'] = True
    context.user_data['selected_purchased_id'] = purchased_plan_id
    name = context.user_data.get('purchased_old_name')
    try:
        await query.message.edit_text(
            "📝 <b>لطفاً یک نام دلخواه دیگر برای اشتراک خود وارد کنید:</b>\n"
            f"نام قبلی سرور شما: {name}",
            parse_mode="HTML"
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise
    context.user_data.pop('purchased_old_name')


async def handle_change_name_purchased_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    purchased_plan_id = context.user_data.get("selected_purchased_id")
    result = q_one("""
        SELECT name, config_data, is_active FROM purchased_plans WHERE id = %s
    """, (purchased_plan_id,))
    name, config_data_json, is_active = result
    if not is_active:
        context.user_data.pop("awaiting_change_name_purchased", None)
        context.user_data.pop("selected_purchased_id", None)
        await update.effective_message.reply_text("❗ این پلن غیرفعال است و امکان تغییر نام ندارد.")
        await show_profile_panel(update, context)
        return
    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        uuid = config_data.get('uuid')
        address = config_data.get("address")
        inbound_id = config_data.get("inbound_id")
        protocol = config_data.get("protocol")
        port = config_data.get("port")
        old_email = name
    except Exception as e:
        return
    result = q_one("SELECT base_url, username, password FROM servers WHERE address = %s", (address,))
    if not result:
        await update.effective_message.reply_text("❌ اطلاعات سرور یافت نشد.")
        return
    base_url, username, password = result
    try:
        inbound_list = await fetch_panel_inbounds(
            base_url=base_url,
            username=username,
            password=password,
        )
    except Exception:
        await update.effective_message.reply_text("❌ ورود به پنل ناموفق بود.")
        return
    found_client = find_panel_client(inbound_list, inbound_id, uuid)
    if not found_client:
        await update.effective_message.reply_text("❌ کلاینت مورد نظر در پنل یافت نشد.")
        return
    old_name = found_client['email'].rsplit('-', 1)[0]
    found_client['email'] = f"{old_name}-{text}"
    try:
        await update_panel_client_payload(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id,
            client_uuid=uuid,
            client_payload=found_client,
        )
    except Exception:
        await update.effective_message.reply_text("❌ تغییر اسم با خطا مواجه شد.")
        return
    new_url = f"{protocol}://{uuid}@{address}:{port}?type=tcp&path=%2F&headerType=http&security=none#{old_name}-{text}"
    config_data["connection_url"] = new_url
    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE purchased_plans
                    SET config_data = %s, name = %s
                    WHERE id = %s
                """, (json.dumps(config_data), text, purchased_plan_id))
    finally:
        pool.putconn(db_conn)
    await update.effective_message.reply_text(
        "✅ اسم کانفیگ با موفقیت تغییر یافت.\n"
        "🔗 <b>لینک جدید:</b>\n"
        f"<code>{new_url}</code>",
        parse_mode="HTML"
    )
    await show_profile_panel(update, context)


async def handle_change_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    purchased_plan_id = callback["id"]
    result = q_one("""
        SELECT config_data, is_active FROM purchased_plans WHERE id = %s
    """, (purchased_plan_id,))

    config_data_json, is_active = result
    if not is_active:
        await query.edit_message_text("❗ این پلن غیرفعال است و امکان تغییر لینک ندارد.")
        return

    try:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        old_uuid = config_data.get("uuid")
        address = config_data.get("address")
        inbound_id = config_data.get("inbound_id")
        protocol = config_data.get("protocol")
        port = config_data.get("port")
    except Exception as e:
        return
    new_uuid = str(uuid.uuid4())
    result = q_one("SELECT base_url, username, password FROM servers WHERE address = %s", (address,))
    if not result:
        await query.edit_message_text("❌ اطلاعات سرور یافت نشد.")
        return
    base_url, username, password = result
    try:
        inbound_list = await fetch_panel_inbounds(
            base_url=base_url,
            username=username,
            password=password,
        )
    except Exception:
        await query.edit_message_text("❌ ورود به پنل ناموفق بود.")
        return
    found_client = find_panel_client(inbound_list, inbound_id, old_uuid)
    if not found_client:
        await query.edit_message_text("❌ کاربر با UUID قبلی یافت نشد.")
        return
    found_client["id"] = new_uuid
    try:
        await update_panel_client_payload(
            base_url=base_url,
            username=username,
            password=password,
            inbound_id=inbound_id,
            client_uuid=old_uuid,
            client_payload=found_client,
        )
    except Exception:
        await query.edit_message_text("❌ تغییر UUID با خطا مواجه شد.")
        return
    new_url = f"{protocol}://{new_uuid}@{address}:{port}?type=tcp&path=%2F&headerType=http&security=none#{found_client['email']}"
    config_data["uuid"] = new_uuid
    config_data["connection_url"] = new_url
    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                cur.execute("""
                    UPDATE purchased_plans
                    SET config_data = %s
                    WHERE id = %s
                """, (json.dumps(config_data), purchased_plan_id))
    finally:
        pool.putconn(db_conn)
    await query.edit_message_text(
        "✅ لینک اتصال با موفقیت تغییر یافت.\n"
        "🔗 <b>لینک جدید:</b>\n"
        f"<code>{new_url}</code>",
        parse_mode="HTML"
    )
    await show_profile_panel(update, context)


async def handle_qr_code_purchased_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    url = context.user_data.get('url')
    if url:
        await send_qr_code(update, context, url)
        context.user_data.pop('url')
    else:
        pass


async def handle_return_user_purchased_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # 👇 همون پیام detail تبدیل میشه به لیست
    page = context.user_data.get(MY_PLANS_PAGE_KEY, 1)
    offset = (page - 1) * PAGE_SIZE

    row = q_one("SELECT id FROM users WHERE user_id = %s", (update.effective_user.id,))
    if not row:
        await q.edit_message_text("❌ کاربر یافت نشد.")
        return
    db_user_id = row[0]

    plans = q_all("""
        SELECT id, name, is_active
        FROM purchased_plans
        WHERE user_id = %s
        ORDER BY purchase_date DESC
        LIMIT %s OFFSET %s
    """, (db_user_id, PAGE_SIZE + 1, offset))

    has_next = len(plans) > PAGE_SIZE
    plans = plans[:PAGE_SIZE]

    buttons = [
        [InlineKeyboardButton(
            f"{'🟢' if is_active else '🔴'} {name or 'بدون نام'}",
            callback_data=f"user_purchased_plan_{pid}"
        )]
        for pid, name, is_active in plans
    ]

    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data="my_plans_prev"))
    if has_next:
        nav.append(InlineKeyboardButton("➡️ بعدی", callback_data="my_plans_next"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="return_my_profile")])

    text = "<b>📁 کانفیگ‌های شما:</b>\n(برای مشاهده جزئیات، یکی را انتخاب کنید)"

    await q.edit_message_text(
        text=text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, message="♦️‍ خوش آمدید به پنل ادمین"):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    set_user_state(context, STATE_BACK_ADMIN_PANEL)
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 مدیریت کاربران", callback_data="admin_panel_users"),
         InlineKeyboardButton("⚙️ تنظیمات سرورها", callback_data="admin_panel_servers")],
        [InlineKeyboardButton("📨 پشتیبانی", callback_data="admin_panel_support"),
         InlineKeyboardButton("🎁 پروموشن", callback_data="promo_panel")],
        [InlineKeyboardButton("🧾 مدیریت طرح‌ها", callback_data="admin_panel_sales"),
         InlineKeyboardButton("🔄 بررسی سینک", callback_data="sync_page:0")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_main_menu")]
    ])

    await update.effective_message.reply_text(message, reply_markup=markup)


async def handle_panel_shortcuts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "buy_panel_open":
        await show_categories_panel(update, context)
        return
    if data == "buy_panel_test":
        await handle_test_server_request(update, context)
        return
    if data == "admin_panel_users":
        await query.edit_message_text(
            "👥 <b>مدیریت کاربران</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 نمایش لیست کاربران", callback_data="users_page_1")],
                [InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="search_user")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
            ])
        )
        return
    if data == "admin_panel_support":
        await query.edit_message_text(
            "📨 <b>مدیریت پیام‌های پشتیبانی</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 همه پیام‌ها", callback_data="all_message_1")],
                [InlineKeyboardButton("❗ پاسخ‌نداده‌ها", callback_data="unanswered_message_1")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
            ])
        )
        return
    if data == "admin_panel_servers":
        await query.edit_message_text(
            "⚙️ <b>مدیریت سرورها</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 نمایش سرورها", callback_data="return_servers"),
                 InlineKeyboardButton("⚙️ سرور تست", callback_data="edit_test_config")],
                [InlineKeyboardButton("📦 تعیین طرح‌های فروش", callback_data="server_plans")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
            ])
        )
        return
    if data == "admin_panel_sales":
        await query.edit_message_text(
            "🧾 <b>مدیریت طرح‌های فروخته‌شده</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("👥 مدیریت کاربران", callback_data="admin_panel_users")],
                [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
            ])
        )
        return


async def handle_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if not is_admin(user_id):
        return
    if text == "👥 مدیریت کاربران":
        loading_msg = await update.message.reply_text(
            "⌛ در حال دریافت اطلاعات...",
            reply_markup=ReplyKeyboardRemove()
        )
        await asyncio.sleep(0.5)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
        except:
            pass
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 نمایش لیست کاربران", callback_data="users_page_1")],
            [InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="search_user")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
        ])

        await update.message.reply_text(
            "👥 <b>مدیریت کاربران:</b>\nلطفاً یک گزینه را انتخاب کنید:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    elif text == "📨 پاسخ به پیام‌های پشتیبانی":
        loading_msg = await update.message.reply_text(
            "⌛ در حال دریافت اطلاعات...",
            reply_markup=ReplyKeyboardRemove()
        )
        await asyncio.sleep(0.5)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
        except:
            pass
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 نمایش تمامی پیام ها", callback_data="all_message_1")],
            [InlineKeyboardButton("🔍 نمایش پیام های پاسخ داده نشده", callback_data="unanswered_message_1")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
        ])

        await update.message.reply_text(
            "👥 <b>پاسخ به پیام‌های پشتیبانی:</b>\nلطفاً یک گزینه را انتخاب کنید:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    if text == "🔙 بازگشت به منوی ادمین":
        await show_admin_panel(update, context)
        return
    if text == "🔙 بازگشت به منوی اصلی":
        await main_menu(update, context)
        return
    if text == "👤 مدیریت ادمین‌ها":
        rows = q_all("""
                SELECT u.user_id, u.name, u.username
                FROM admins a
                JOIN users u ON a.user_id = u.user_id
                ORDER BY a.id ASC
            """)

        if not rows:
            await update.message.reply_text("❌ هیچ ادمینی ثبت نشده است.")
            return

        text = "👮‍♂️ لیست ادمین‌ها:\n\n"
        for user_id, name, username in rows:
            text += f"🆔 <b>User ID:</b> {user_id}\n"
            text += f"👤 <b>نام:</b> {name}\n"
            text += f"🔗 <b>یوزرنیم:</b> @{username if username else '-'}\n"
            text += "──────────────────────\n"

        await update.message.reply_text(text, parse_mode="HTML")
        buttons = [
            [KeyboardButton("➕ افزودن ادمین"), KeyboardButton("➖ حذف ادمین")],
            [KeyboardButton("🔙 بازگشت به منوی ادمین")]
        ]
        markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)

        await update.message.reply_text("یکی از گزیته ها را انتخاب کنید", reply_markup=markup)
    if text == "➕ افزودن ادمین":
        context.user_data["awaiting_admin_id"] = True
        await update.message.reply_text("🆔 لطفاً آیدی عددی کاربر را برای افزودن ارسال کنید:")
        return
    elif text == "➖ حذف ادمین":
        context.user_data["delete_admin_id"] = True
        await update.message.reply_text("🆔 لطفاً آیدی عددی کاربر را برای حذف ارسال کنید:")
        return

    if context.user_data.get("awaiting_admin_id"):
        try:
            new_admin_id = int(text)
            q_exec("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (new_admin_id,))
            await update.message.reply_text(f" آیدی {new_admin_id}✅ با موفقیت اضافه شد.")
        except ValueError:
            await update.message.reply_text("⚠️ لطفاً فقط عدد وارد کنید.")
        context.user_data["awaiting_admin_id"] = False
        return

    if context.user_data.get("delete_admin_id"):
        try:
            delete_admin_id = int(text)

            admin_rows = q_all("SELECT user_id FROM admins ORDER BY added_at ASC LIMIT 2")

            if (user_id,) in admin_rows:
                if delete_admin_id == user_id:
                    await update.message.reply_text("⛔️ نمی‌توانید خودتان را حذف کنید.")
                else:
                    q_exec("DELETE FROM admins WHERE user_id = %s", (delete_admin_id,))
                    await update.message.reply_text(f" آیدی {delete_admin_id}✅  با موفقیت حذف شد.")
            else:
                await update.message.reply_text("⚠️ فقط اونر و ادمین اصلی مجاز به این عملیات هستند.")
        except ValueError:
            await update.message.reply_text("⚠️ لطفاً فقط عدد وارد کنید.")
        context.user_data["delete_admin_id"] = False
        return

    if text == "⚙️ تنظیمات سرور‌ها":
        set_user_state(context, STATE_BACK_SETUP_SERVERS)
        buttons = [
            [KeyboardButton("➕ افزودن سرور"), KeyboardButton("📊 نمایش سرورها")],
            [KeyboardButton('⚙️ تنظیمات سرور تست'), KeyboardButton("📦 تعیین طرح‌های فروش")],
            [KeyboardButton("↩️ بازگشت")]
        ]
        markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        await update.message.reply_text("لطفاً یکی از گزینه‌ها را انتخاب کنید: 🔽", reply_markup=markup)
        return
    if text == "⚙️ تنظیمات سرور تست":
        config = q_one("""
                SELECT t.is_active, s.name, t.inbound_id, t.traffic_gb, t.duration_days
                FROM test_server_config t
                JOIN servers s ON t.server_id = s.id
                LIMIT 1
            """)

        if config:
            is_active, name, inbound_id, traffic_gb, duration_days = config
            message = (
                f"📦 <b>تنظیمات فعلی سرور تست:</b>\n\n"
                f"✅ <b>فعال:</b> {'بله ✅' if is_active else 'خیر ❌'}\n"
                f"🌐 <b>نام سرور:</b> {name}\n"
                f"🆔 <b>Inbound ID:</b> {inbound_id}\n"
                f"💾 <b>حجم:</b> {traffic_gb}GB\n"
                f"⏳ <b>مدت زمان:</b> {duration_days} روز\n\n"
                f"⚙️ برای ویرایش یا حذف، یکی از گزینه‌ها را انتخاب کنید."
            )
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📝 ویرایش", callback_data="edit_test_config"),
                    InlineKeyboardButton("➖ حذف", callback_data="delete_test_config")]
            ])
            await update.message.reply_text(message, reply_markup=keyboard, parse_mode='HTML')
        else:
            set_timed_value(context, "test_config", {"step": "is_active"})
            await update.message.reply_text("🟢 آیا مایلید سرور تست فعال باشد؟\n\n✅ بله / ❌ خیر را وارد کنید.")

    test_config = get_timed_value(context, "test_config")
    if test_config:
        step = test_config.get("step")

        if step == "is_active":
            if text.lower() in ["بله", "yes"]:
                test_config["is_active"] = True
                test_config["step"] = "server_name"
                set_timed_value(context, "test_config", test_config)
                await update.message.reply_text("📡 لطفاً نام سروری که می‌خواهید تنظیم کنید را وارد نمایید:")
                return

            elif text.lower() in ["خیر", "نه", "no"]:
                test_config["is_active"] = False
                if all(test_config.get(k) is not None for k in ("server_id", "inbound_id", "traffic_gb", "duration_days")):
                    q_exec("DELETE FROM test_server_config")
                    q_exec("""
                        INSERT INTO test_server_config (is_active, server_id, inbound_id, traffic_gb, duration_days)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (
                        False,
                        test_config["server_id"],
                        test_config["inbound_id"],
                        test_config["traffic_gb"],
                        test_config["duration_days"]
                    ))
                    clear_timed_value(context, "test_config")
                    await update.message.reply_text("✅ سرور تست با موفقیت غیرفعال شد.")
                else:
                    clear_timed_value(context, "test_config")
                    await update.message.reply_text("ℹ️ فرایند تنظیم سرور تست لغو شد.")
                return

            else:
                await update.message.reply_text("⚠️ لطفاً فقط 'بله' یا 'خیر' وارد کنید.")
                return

        elif step == "server_name":
            server_name = sanitize_text_input(text, max_len=128, field_name="test server name")
            server_row = q_one("SELECT id FROM servers WHERE name = %s", (server_name,))
            if not server_row:
                await update.message.reply_text("❗️سروری با این نام پیدا نشد. لطفاً نام صحیح سرور را دوباره وارد کنید:")
                return
            test_config["server_id"] = server_row[0]
            test_config["server_name"] = server_name
            test_config["step"] = "inbound_id"
            set_timed_value(context, "test_config", test_config)
            await update.message.reply_text("🆔 لطفاً ID اینباند را وارد کنید:")
            return

        elif step == "inbound_id":
            try:
                inbound_id = int(text)
                if inbound_id <= 0:
                    raise ValueError
                test_config["inbound_id"] = inbound_id
            except ValueError:
                await update.message.reply_text("⚠️ لطفاً فقط عدد وارد کنید.")
                return
            test_config["step"] = "traffic_gb"
            set_timed_value(context, "test_config", test_config)
            await update.message.reply_text("🔢 لطفاً حجم (به گیگابایت) را وارد کنید:")
            return

        elif step == "traffic_gb":
            try:
                traffic_gb = int(text)
                if traffic_gb <= 0:
                    raise ValueError
                test_config["traffic_gb"] = traffic_gb
            except ValueError:
                await update.message.reply_text("⚠️ لطفاً یک عدد صحیح بزرگ‌تر از صفر وارد کنید.")
                return
            test_config["step"] = "duration_days"
            set_timed_value(context, "test_config", test_config)
            await update.message.reply_text("⏳ لطفاً مدت زمان (به روز) را وارد کنید:")
            return

        elif step == "duration_days":
            try:
                duration_days = int(text)
                if duration_days <= 0:
                    raise ValueError
                test_config["duration_days"] = duration_days
            except ValueError:
                await update.message.reply_text("⚠️ لطفاً یک عدد صحیح بزرگ‌تر از صفر وارد کنید.")
                return

            q_exec("DELETE FROM test_server_config")

            q_exec("""
                INSERT INTO test_server_config (is_active, server_id, inbound_id, traffic_gb, duration_days)
                VALUES (%s, %s, %s, %s, %s)
            """, (
                bool(test_config["is_active"]),
                test_config["server_id"],
                test_config["inbound_id"],
                test_config["traffic_gb"],
                test_config["duration_days"]
            ))

            await update.message.reply_text("✅ تنظیمات سرور تست ذخیره شد.")
            clear_timed_value(context, "test_config")
            return

    if "edit_category" in context.user_data:
        step = context.user_data["edit_category"].get("step")

        if step == "name":
            if text.lower() == "skip":
                pass
            else:
                context.user_data["edit_category"]["name"] = text
            context.user_data["edit_category"]["step"] = "emoji"
            await update.message.reply_text(
                text=f"""
                📝 <b>ویرایش دسته‌بندی</b>

                📌 <b>اموجی فعلی:</b> <i>{context.user_data["edit_category"]["emoji"]}</i>

                ✏️ اگر مایل به تغییر اموجی هستید، لطفاً اموجی جدید را وارد کنید.
                ⏭ در غیر این صورت، کلمه <code>skip</code> را ارسال نمایید.
                        """,
                parse_mode="HTML"
            )
            return
        if step == "emoji":
            if text.lower() != "skip":
                context.user_data["edit_category"]["emoji"] = text

            new_category = context.user_data["edit_category"]
            category_id = new_category["id"]
            new_name = new_category.get("name")
            new_emoji = new_category.get("emoji")

            q_exec(
                "UPDATE categories SET name = %s, emoji = %s WHERE id = %s",
                (new_name, new_emoji, category_id)
            )
            context.user_data.pop("edit_category", None)

            await update.message.reply_text(
                text=f"✅ دسته‌بندی با موفقیت ویرایش شد.\n\n<b>نام جدید:</b> {new_name}\n<b>ایموجی:</b> {new_emoji}",
                parse_mode="HTML"
            )
            await show_categories_menu(update, context)
            return
    if text == "➕ افزودن سرور":
        context.user_data["adding_server"] = {"step": "name"}
        await update.message.reply_text("🔠 نام سرور را وارد کنید:", reply_markup=ReplyKeyboardRemove())
        return
    if "adding_server" in context.user_data:
        step = context.user_data["adding_server"]["step"]
        server_data = context.user_data["adding_server"]

        if step == "name":
            server_data["name"] = text
            server_data["step"] = "base_url"
            await update.message.reply_text("✏️ آدرس Base URL را وارد کنید:")
            return

        elif step == "base_url":
            server_data["base_url"] = text
            server_data["step"] = "username"
            await update.message.reply_text("👤 نام کاربری پنل را وارد کنید:")
            return

        elif step == "username":
            server_data["username"] = text
            server_data["step"] = "password"
            await update.message.reply_text("🔐 رمز عبور را وارد کنید:")
            return

        elif step == "password":
            server_data["password"] = text
            server_data["step"] = "location"
            await update.message.reply_text("🌍 لطفاً موقعیت سرور را وارد کنید:")
            return

        elif step == "location":
            server_data["location"] = text
            server_data["step"] = "max_traffic"
            await update.message.reply_text("🔢 حداکثر ترافیک (گیگابایت) را وارد کنید:")
            return

        elif step == "max_traffic":
            try:
                server_data["max_traffic"] = float(text)
            except ValueError:
                await update.message.reply_text("⚠️ عدد نامعتبر است. لطفاً عدد وارد کنید.")
                return

            server_data["step"] = "max_users"
            await update.message.reply_text("👨‍👩‍👦‍👦 حداکثر تعداد کاربران را وارد کنید:")
            return

        elif step == "max_users":
            try:
                server_data["max_users"] = int(text)
            except ValueError:
                await update.message.reply_text("⚠️ عدد نامعتبر است. لطفاً عدد صحیح وارد کنید.")
                return
            server_data['step'] = "address"
            await update.message.reply_text("📝 آدرس مورد نظر را وارد کنید: ")
            return
        elif step == "address":
            server_data["address"] = text

            q_exec("""
                INSERT INTO servers (name, base_url, username, password, location, max_traffic_gb, max_users, address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                server_data["name"],
                server_data["base_url"],
                server_data["username"],
                server_data["password"],
                server_data["location"],
                server_data["max_traffic"],
                server_data["max_users"],
                server_data['address']
            ))

            await update.message.reply_text("✅ سرور با موفقیت افزوده شد.")
            del context.user_data["adding_server"]
            await show_admin_panel(update, context, message="🌐 شما به پنل ادمین برگشتید")
            return

    if text == "📊 نمایش سرورها":
        servers = q_all("""
            SELECT name FROM servers ORDER BY created_at ASC
        """)

        loading_msg = await update.message.reply_text(
            "⌛ در حال دریافت لیست سرورها...",
            reply_markup=ReplyKeyboardRemove()
        )
        await asyncio.sleep(0.5)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
        except:
            pass

        if not servers:
            await update.message.reply_text("❌ هیچ سروری یافت نشد.")
            return

        keyboard = []
        for i, row in enumerate(servers):
            server_name = row[0]
            keyboard.append([InlineKeyboardButton(server_name, callback_data=f"server_{i}")])
        keyboard.append([InlineKeyboardButton('بازگشت به منوی اصلی', callback_data="return_setup_server_menu")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await update.message.reply_text("یکی از سرورها را انتخاب کنید: 🔽", reply_markup=reply_markup)
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)

    if text == "📦 تعیین طرح‌های فروش":
        categories = q_all("SELECT id, name, emoji FROM categories")

        buttons = []

        if categories:
            for cat_id, name, emoji in categories:
                buttons.append([
                    InlineKeyboardButton(f"{name} {emoji}", callback_data=f"admin_view_category_{cat_id}")
                ])
        buttons.append([InlineKeyboardButton("➕ افزودن دسته‌بندی", callback_data="add_category")])
        buttons.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="return_setup_server_menu")])

        keyboard = InlineKeyboardMarkup(buttons)
        loading_msg = await update.message.reply_text(
            "⌛ در حال دریافت لیست طرح‌های فروش...",
            reply_markup=ReplyKeyboardRemove()
        )
        await asyncio.sleep(0.5)
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=loading_msg.message_id)
        except:
            pass
        msg = await update.message.reply_text(
            "<b>📦 دسته‌بندی‌های طرح‌های فروش:</b>\n\n"
            "لطفاً یکی از دسته‌ها را انتخاب کنید یا دسته‌ای جدید اضافه نمایید.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    if text == "🔄 بررسی ناهماهنگی DB و پنل":
        await admin_sync_check(update, context)
        return
    if text == "🎁 پروموشن‌ها":
        await show_promo_panel(update, context)
        return


def get_active_promo(cur):
    cur.execute("""
        UPDATE promo_settings
        SET enabled = FALSE
        WHERE enabled = TRUE
          AND end_at IS NOT NULL
          AND end_at <= NOW()
    """)

    cur.execute("""
        SELECT id, percent
        FROM promo_settings
        WHERE enabled = TRUE
          AND (end_at IS NULL OR end_at > NOW())
        ORDER BY id DESC
        LIMIT 1
    """)
    row = cur.fetchone()
    if not row:
        return None
    promo_id, percent = row
    return {"id": promo_id, "percent": float(percent or 0)}


def apply_promo_and_calculate_final_price(cur, plan_price, db_user_id, base_gb):
    promo = get_active_promo(cur)
    base_gb = int(base_gb)
    max_gb = apply_traffic_promo(base_gb, promo["percent"]) if promo else base_gb
    bonus_gb = max_gb - base_gb

    cur.execute("SELECT COALESCE(discount_percentage, 0) FROM users WHERE id = %s", (db_user_id,))
    discount_percentage = float(cur.fetchone()[0] or 0)

    final_price = int(plan_price)
    if discount_percentage > 0:
        final_price = int(int(plan_price) * (1 - discount_percentage / 100))

    return final_price, int(base_gb), bonus_gb, max_gb


async def show_promo_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_promo_settings()  # {"enabled": bool, "percent": float, "end_at": datetime|None}

    now_utc = datetime.now(timezone.utc)
    active = False
    if s.get("enabled") and float(s.get("percent") or 0) > 0:
        if s.get("end_at") is None or now_utc < s["end_at"]:
            active = True

    status = "✅ فعال" if active else "⛔ خاموش"
    percent_txt = f"{float(s.get('percent') or 0):g}%"

    if s.get("end_at"):
        tehran = pytz.timezone("Asia/Tehran")
        end_txt = s["end_at"].astimezone(tehran).strftime("%Y-%m-%d %H:%M")
    else:
        end_txt = "—"

    text = (
        "🎁 <b>پروموشن افزایش حجم</b>\n\n"
        f"وضعیت: <b>{status}</b>\n"
        f"درصد: <b>{percent_txt}</b>\n"
        f"تا: <b>{end_txt}</b>\n\n"
        "⏰ زمان‌ها به وقت ایران هستند."
    )

    buttons = [
        [InlineKeyboardButton("➕ ساخت پرومو", callback_data="promo_create")],
    ]
    if active:
        buttons.append([InlineKeyboardButton("⛔ خاموش کردن", callback_data="promo_off")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت به پنل ادمین", callback_data="promo_back_admin")])

    kb = InlineKeyboardMarkup(buttons)

    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=ReplyKeyboardRemove()
    )
    await update.effective_message.reply_text("گزینه‌ها:", reply_markup=kb)


async def handle_promo_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    q = update.callback_query
    data = q.data
    await q.answer()

    if data == "promo_back_admin":
        try:
            await q.message.delete()
        except:
            pass
        await show_admin_panel(update, context)
        return

    if data == "promo_create":
        set_user_state(context, STATE_AWAITING_PROMO_PERCENT)
        await q.message.reply_text("🔢 درصد پرومو را وارد کن. مثال: 25 یا 25%")
        return

    if data == "promo_off":
        confirm_text = (
            "⛔ <b>خاموش کردن پروموشن</b>\n\n"
            "مطمئنی می‌خوای خاموشش کنی؟\n"
            "این فقط روی خریدهای بعدی اثر می‌ذاره."
        )
        confirm_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ بله، خاموش کن", callback_data="promo_off_yes"),
            InlineKeyboardButton("❌ نه", callback_data="promo_off_no"),
        ]])
        await q.edit_message_text(confirm_text, parse_mode="HTML", reply_markup=confirm_kb)
        return

    if data == "promo_off_no":
        # برگشت به صفحه وضعیت
        await q.message.delete()
        await show_promo_panel(update, context)
        return

    if data == "promo_off_yes":
        db_conn = pool.getconn()
        try:
            with db_conn:
                with db_conn.cursor() as cur:
                    # اول expire ها رو خاموش کن (برای تمیزی)
                    cur.execute("""
                        UPDATE promo_settings
                        SET enabled = FALSE
                        WHERE enabled = TRUE
                          AND end_at IS NOT NULL
                          AND end_at <= NOW()
                    """)
                    # بعد پروموی فعال رو خاموش کن (فقط enabled=false)
                    cur.execute("""
                        UPDATE promo_settings
                        SET enabled = FALSE
                        WHERE enabled = TRUE
                    """)
        finally:
            pool.putconn(db_conn)

        await q.message.delete()
        await show_promo_panel(update, context)
        return


def _parse_percent(text: str) -> float:
    t = (text or "").strip().replace("%", "")
    return float(t)


def _parse_end_tehran_to_utc(text: str) -> datetime:
    tehran = pytz.timezone("Asia/Tehran")
    t = (text or "").translate(PERSIAN_DIGITS).strip()
    dt_local = datetime.strptime(t, "%Y-%m-%d %H:%M")
    dt_local = tehran.localize(dt_local, is_dst=None)
    return dt_local.astimezone(timezone.utc)


async def handle_promo_percent_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    try:
        percent = _parse_percent(update.message.text)
        if not (0 < percent <= 100):
            await update.message.reply_text("❗ درصد باید بین 1 تا 100 باشد. مثال: 25")
            return
    except Exception:
        await update.message.reply_text("❗ فرمت درصد درست نیست. مثال: 25 یا 25%")
        return

    set_timed_value(context, "promo_draft", {"percent": percent})
    set_user_state(context, STATE_AWAITING_PROMO_END)
    await update.message.reply_text(
        "🕒 تاریخ پایان را به وقت ایران وارد کن:\n"
        "<code>YYYY-MM-DD HH:MM</code>\n"
        "مثال: <code>2026-02-10 23:59</code>",
        parse_mode="HTML"
    )


async def handle_promo_end_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    draft = get_timed_value(context, "promo_draft", {}) or {}
    percent = float(draft.get("percent") or 0)
    if percent <= 0:
        clear_timed_value(context, "promo_draft")
        clear_user_state(context)
        await update.message.reply_text("❌ خطا: درصد مشخص نیست. دوباره از پنل شروع کن.")
        return

    try:
        end_utc = _parse_end_tehran_to_utc(update.message.text)
        if end_utc <= datetime.now(timezone.utc):
            await update.message.reply_text("❗ تاریخ پایان باید در آینده باشد. دوباره بفرست.")
            return
    except Exception:
        await update.message.reply_text("❗ فرمت تاریخ درست نیست. مثال: 2026-02-10 23:59")
        return

    # INSERT promo جدید فقط وقتی promo فعال نداریم
    db_conn = pool.getconn()
    try:
        with db_conn:
            with db_conn.cursor() as cur:
                # expire های گذشته رو خاموش کن
                cur.execute("""
                    UPDATE promo_settings
                    SET enabled = FALSE
                    WHERE enabled = TRUE
                      AND end_at IS NOT NULL
                      AND end_at <= NOW()
                """)

                # اگر هنوز promo فعال داریم، اجازه ساخت نده
                cur.execute("SELECT 1 FROM promo_settings WHERE enabled = TRUE LIMIT 1")
                if cur.fetchone():
                    clear_timed_value(context, "promo_draft")
                    clear_user_state(context)
                    await update.message.reply_text("⛔ پروموی فعال دارید. اول خاموشش کن، بعد پروموی جدید بساز.")
                    return

                # ✅ ساخت id جدید (max+1)
                cur.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM promo_settings")
                new_id = int(cur.fetchone()[0])

                # ✅ INSERT با id جدید
                cur.execute("""
                    INSERT INTO promo_settings (id, enabled, percent, end_at, updated_by, updated_at)
                    VALUES (%s, TRUE, %s, %s, %s, NOW())
                """, (new_id, percent, end_utc, update.effective_user.id))
    finally:
        pool.putconn(db_conn)

    await update.message.reply_text(f"✅ پرومو ساخته و فعال شد. (ID: {new_id})")
    clear_timed_value(context, "promo_draft")
    clear_user_state(context)
    await show_promo_panel(update, context)


def is_sync_ignored(plan_id: int, issue_type: str) -> bool:
    row = q_one(
        "SELECT 1 FROM plan_sync_ignores WHERE plan_id=%s AND issue_type=%s",
        (plan_id, issue_type)
    )
    return bool(row)


def add_sync_ignore(plan_id: int, issue_type: str, admin_tg_id: int):
    q_exec(
        """
        INSERT INTO plan_sync_ignores (plan_id, issue_type, ignored_by)
        VALUES (%s, %s, %s)
        ON CONFLICT (plan_id, issue_type) DO UPDATE
        SET ignored_by = EXCLUDED.ignored_by, ignored_at = NOW()
        """,
        (plan_id, issue_type, admin_tg_id)
    )


def scan_sync_mismatches(limit_each=200, max_collect=100):
    """
    خروجی: لیست آیتم‌ها
    item = {
      "issue_type": "A" or "B",
      "plan_id": ...,
      "tg_id": ...,
      "name": ...,
      "reason": ...,
      "deact_at": ...,
      "deact_by": ...,
      "expiry": ...,
      "max_gb": ...,
      "address": ...,
      "inbound_id": ...,
      "uuid": ...,
      "panel_status": ... (optional)
    }
    """
    inactive_rows = q_all("""
        SELECT
          pp.id, pp.config_data, pp.name,
          pp.expiry_date, pp.max_traffic_gb,
          pp.deactivated_reason, pp.deactivated_at, pp.deactivated_by,
          u.user_id AS telegram_id
        FROM purchased_plans pp
        JOIN users u ON pp.user_id = u.id
        WHERE pp.is_active = FALSE
        ORDER BY pp.id DESC
        LIMIT %s
    """, (limit_each,))

    active_rows = q_all("""
        SELECT
          pp.id, pp.config_data, pp.name,
          pp.expiry_date, pp.max_traffic_gb,
          pp.deactivated_reason, pp.deactivated_at, pp.deactivated_by,
          u.user_id AS telegram_id
        FROM purchased_plans pp
        JOIN users u ON pp.user_id = u.id
        WHERE pp.is_active = TRUE
        ORDER BY pp.id DESC
        LIMIT %s
    """, (limit_each,))

    server_cache = {}  # address -> inbound_list or None

    def get_inbounds(address: str):
        if address in server_cache:
            return server_cache[address]
        srv = q_one("SELECT base_url, username, password FROM servers WHERE address=%s", (address,))
        if not srv:
            server_cache[address] = None
            return None
        base_url, username, password = srv
        s = requests.Session()
        try:
            login = s.post(f"{base_url}/login", json={"username": username, "password": password}, timeout=REQ_TIMEOUT)
            if not login.ok or not (login.json() or {}).get("success"):
                server_cache[address] = None
                return None
            inbound_list = s.get(f"{base_url}/panel/api/inbounds/list", timeout=REQ_TIMEOUT).json()
            server_cache[address] = inbound_list
            return inbound_list
        except Exception:
            server_cache[address] = None
            return None

    def find_client(inbound_list: dict, inbound_id, uuid: str):
        for inbound in (inbound_list.get("obj", []) or []):
            if str(inbound.get("id")) == str(inbound_id):
                try:
                    settings = json.loads(inbound.get("settings", "{}"))
                except Exception:
                    settings = {}
                clients = settings.get("clients", []) or []
                for c in clients:
                    if c.get("id") == uuid:
                        return ("found", c.get("enable", True))
                return ("missing", None)
        return ("no_inbound", None)

    items = []

    # A) DB inactive ولی پنل active
    for (plan_id, cfg_json, name, expiry, max_gb, reason, deact_at, deact_by, tg_id) in inactive_rows:
        if is_sync_ignored(plan_id, "A"):
            continue
        try:
            cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else (cfg_json or {})
            uuid = cfg.get("uuid")
            address = cfg.get("address")
            inbound_id = cfg.get("inbound_id")
            if not (uuid and address and inbound_id):
                continue
        except Exception:
            continue

        inbound_list = get_inbounds(address)
        if not inbound_list:
            continue

        st, enabled = find_client(inbound_list, inbound_id, uuid)
        if st == "found" and enabled is True:
            items.append({
                "issue_type": "A",
                "plan_id": plan_id,
                "tg_id": tg_id,
                "name": name or "-",
                "reason": reason or "unknown",
                "deact_at": deact_at,
                "deact_by": deact_by or "-",
                "expiry": expiry,
                "max_gb": max_gb,
                "address": address,
                "inbound_id": inbound_id,
                "uuid": uuid,
                "panel_status": "active",
            })
            if len(items) >= max_collect:
                return items

    # B) DB active ولی پنل missing
    for (plan_id, cfg_json, name, expiry, max_gb, reason, deact_at, deact_by, tg_id) in active_rows:
        if is_sync_ignored(plan_id, "B"):
            continue
        try:
            cfg = json.loads(cfg_json) if isinstance(cfg_json, str) else (cfg_json or {})
            uuid = cfg.get("uuid")
            address = cfg.get("address")
            inbound_id = cfg.get("inbound_id")
            if not (uuid and address and inbound_id):
                continue
        except Exception:
            continue

        inbound_list = get_inbounds(address)
        if not inbound_list:
            continue

        st, enabled = find_client(inbound_list, inbound_id, uuid)
        if st in ("missing", "no_inbound"):
            items.append({
                "issue_type": "B",
                "plan_id": plan_id,
                "tg_id": tg_id,
                "name": name or "-",
                "reason": reason or "unknown",
                "deact_at": deact_at,
                "deact_by": deact_by or "-",
                "expiry": expiry,
                "max_gb": max_gb,
                "address": address,
                "inbound_id": inbound_id,
                "uuid": uuid,
                "panel_status": "missing" if st == "missing" else "no_inbound",
            })
            if len(items) >= max_collect:
                return items

    return items


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%d %H:%M") if dt else "-"


def fmt_gb(x):
    if x is None:
        return "-"
    try:
        return f"{float(x):g} GB"
    except Exception:
        return f"{x} GB"


def issue_title(issue_type: str) -> str:
    if issue_type == "A":
        return "DB: غیرفعال | Panel: فعال"
    if issue_type == "B":
        return "DB: فعال | Panel: پیدا نشد"
    return "DB/Panel: نامشخص"


def render_sync_page(items, page: int):
    total = len(items)
    max_page = max(0, (total - 1) // PAGE_SIZE)
    page = max(0, min(page, max_page))

    start = page * PAGE_SIZE
    end = min(start + PAGE_SIZE, total)
    chunk = items[start:end]

    lines = []
    lines.append("🧾 <b>بررسی ناهماهنگی DB ↔ پنل</b>")
    lines.append(f"صفحه: <b>{page + 1}</b> از <b>{max_page + 1}</b> | تعداد: <b>{total}</b>")
    lines.append("")

    if not chunk:
        lines.append("✅ موردی برای نمایش نیست.")
    else:
        for i, it in enumerate(chunk, 1):
            tag = it["issue_type"]
            title = issue_title(tag)
            lines.append(
                f"{i}) <b>[{tag}]</b> <b>{title}</b>\n"
                f"   PlanID: <code>{it['plan_id']}</code> | UserTG: <code>{it['tg_id']}</code>\n"
                f"   Name: <b>{it['name']}</b>\n"
                f"   Reason: <code>{it['reason']}</code> | Expiry: <code>{fmt_dt(it['expiry'])}</code> | Max: <code>{fmt_gb(it['max_gb'])}</code>\n"
                f"   Server: <code>{it['address']}</code> | Inbound: <code>{it['inbound_id']}</code>\n"
                "────────────"
            )

    # دکمه‌های ۱..۵
    num_buttons = []
    for idx_in_page in range(0, len(chunk)):
        global_index = start + idx_in_page
        num_buttons.append(
            InlineKeyboardButton(str(idx_in_page + 1), callback_data=f"sync_item:{global_index}")
        )

    nav_row = []
    nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data="sync_page:prev"))
    nav_row.append(InlineKeyboardButton("➡️ بعدی", callback_data="sync_page:next"))
    nav_row.append(InlineKeyboardButton("✖️ بستن", callback_data="sync_close"))

    keyboard = []
    if num_buttons:
        keyboard.append(num_buttons)
    keyboard.append(nav_row)

    return "\n".join(lines), InlineKeyboardMarkup(keyboard), page, max_page


def render_sync_detail(it, global_index: int):
    tag = it["issue_type"]
    miss = it.get("panel_status", "-")

    text = (
        f"🔎 <b>جزئیات مورد</b>\n"
        f"نوع: <b>{tag}</b>\n"
        f"PlanID: <code>{it['plan_id']}</code>\n"
        f"UserTG: <code>{it['tg_id']}</code>\n"
        f"Name: <b>{it['name']}</b>\n"
        f"Reason(DB): <code>{it['reason']}</code>\n"
        f"Deact By: <code>{it.get('deact_by', '-')}</code> | At: <code>{fmt_dt(it.get('deact_at'))}</code>\n"
        f"Expiry(DB): <code>{fmt_dt(it['expiry'])}</code>\n"
        f"MaxTraffic(DB): <code>{fmt_gb(it['max_gb'])}</code>\n"
        f"Server: <code>{it['address']}</code>\n"
        f"Inbound: <code>{it['inbound_id']}</code>\n"
        f"UUID: <code>{it['uuid']}</code>\n"
        f"PanelStatus: <code>{miss}</code>\n"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬅️ بازگشت", callback_data="sync_back"),
            InlineKeyboardButton("🔄 آپدیت همین مورد", callback_data=f"sync_recheck:{global_index}"),
        ],
        [
            InlineKeyboardButton("🚫 بیخیال برای همیشه", callback_data=f"sync_ignore:{global_index}")
        ],
        [
            InlineKeyboardButton("✖️ بستن", callback_data="sync_close")
        ]
    ])
    return text, kb


async def handle_sync_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    items = context.user_data.get(SYNC_CACHE_KEY, [])
    page = context.user_data.get(SYNC_PAGE_KEY, 0)

    if data == "sync_close":
        await q.edit_message_text("✅ بسته شد.")
        return

    if data == "sync_back":
        text, kb, page, _ = render_sync_page(items, page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("sync_page:"):
        if data.endswith("next"):
            page += 1
        else:
            page -= 1
        context.user_data[SYNC_PAGE_KEY] = page
        text, kb, page, _ = render_sync_page(items, page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("sync_item:"):
        idx = int(data.split(":")[1])
        if idx < 0 or idx >= len(items):
            await q.answer("مورد نامعتبر", show_alert=True)
            return
        it = items[idx]
        text, kb = render_sync_detail(it, idx)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("sync_ignore:"):
        idx = int(data.split(":")[1])
        if idx < 0 or idx >= len(items):
            await q.answer("مورد نامعتبر", show_alert=True)
            return
        it = items[idx]
        add_sync_ignore(it["plan_id"], it["issue_type"], update.effective_user.id)

        # از لیست حذف کن و برگرد به صفحه
        items.pop(idx)
        context.user_data[SYNC_CACHE_KEY] = items
        text, kb, page, _ = render_sync_page(items, page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if data.startswith("sync_recheck:"):
        # فعلاً ساده: فقط کل اسکن رو refresh می‌کنیم
        # (اگر خواستی، بعداً دقیقاً همین یک مورد رو recheck می‌کنیم)
        items = scan_sync_mismatches(limit_each=200, max_collect=200)
        context.user_data[SYNC_CACHE_KEY] = items
        text, kb, page, _ = render_sync_page(items, page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        return


async def admin_sync_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    await msg.reply_text("🔍 در حال بررسی...")

    items = scan_sync_mismatches(limit_each=200, max_collect=200)
    context.user_data[SYNC_CACHE_KEY] = items
    context.user_data[SYNC_PAGE_KEY] = 0

    text, kb, page, max_page = render_sync_page(items, 0)
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb)


MESSAGE_PER_PAGE = 5


async def handle_user_message_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    internal_user_id = int(query.data.rsplit("_", 1)[-1])

    res = q_one("SELECT user_id FROM users WHERE id=%s", (internal_user_id,))
    if not res:
        await query.message.reply_text("❌ کاربر پیدا نشد.")
        return

    target_tg_id = res[0]

    set_user_state(context, STATE_AWAITING_ADMIN_USER_MESSAGE)
    set_timed_value(context, "target_user_internal_id", internal_user_id)
    set_timed_value(context, "target_user_tg_id", target_tg_id)

    await query.message.reply_text("✍️ متن پیام را ارسال کنید تا برای کاربر فرستاده شود:")


async def handle_show_all_support_messages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_show_all_support_messages_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    page = callback["page"]
    offset = (page - 1) * MESSAGE_PER_PAGE

    total_messages = q_one("SELECT COUNT(*) FROM support_messages")[0]
    total_pages = (total_messages + MESSAGE_PER_PAGE - 1) // MESSAGE_PER_PAGE

    rows = q_all("""
        SELECT id, user_id, username, message_text, timestamp, status
        FROM support_messages
        ORDER BY timestamp DESC
        LIMIT %s OFFSET %s
    """, (MESSAGE_PER_PAGE, offset))

    if not rows:
        await query.edit_message_text("❌ هیچ پیام پشتیبانی‌ای یافت نشد.")
        return
    lines = [f"📨 <b>پیام‌های پشتیبانی (صفحه {page}/{total_pages}):</b>\n"]
    reply_buttons = []

    for idx, (msg_id, user_id, username, message_text, timestamp, status) in enumerate(rows, start=1):
        username_display = f"@{username}" if username else f"ID: {user_id}"
        status_display = "✅ پاسخ داده شده" if status == "replied" else "❗ بدون پاسخ"

        lines.append(
            f"<b>{idx}. کاربر:</b> {username_display}\n"
            f"📬 <b>پیام:</b> {message_text}\n"
            f"🕒 <b>زمان ارسال:</b> {timestamp.strftime('%H:%M %Y-%m-%d')}\n"
            f"{status_display}\n"
        )
        if status != "replied":
            reply_buttons.append(
                InlineKeyboardButton(f"✍️ پاسخ به شماره {idx}", callback_data=f"reply_to_{msg_id}")
            )

    text = "\n".join(lines)
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"all_message_{page - 1}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️ بعدی", callback_data=f"all_message_{page + 1}"))
    reply_button_rows = [
        reply_buttons[i:i + 2] for i in range(0, len(reply_buttons), 2)
    ]
    reply_button_rows.insert(0, nav_row)
    reply_button_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")])

    markup = InlineKeyboardMarkup(reply_button_rows)

    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)


async def handle_unanswered_support_messages_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_unanswered_support_messages_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    page = callback["page"]
    offset = (page - 1) * MESSAGE_PER_PAGE

    total_messages = q_one("SELECT COUNT(*) FROM support_messages WHERE status = 'pending'")[0]
    total_pages = (total_messages + MESSAGE_PER_PAGE - 1) // MESSAGE_PER_PAGE

    rows = q_all("""
        SELECT id, user_id, username, message_text, timestamp, status
        FROM support_messages
        WHERE status = 'pending'
        ORDER BY timestamp DESC
        LIMIT %s OFFSET %s
    """, (MESSAGE_PER_PAGE, offset))

    if not rows:
        await query.edit_message_text("❌ هیچ پیام پشتیبانی‌ای یافت نشد.")
        return
    lines = [f"📨 <b>پیام‌های پشتیبانی (صفحه {page}/{total_pages}):</b>\n"]
    reply_buttons = []

    for idx, (msg_id, user_id, username, message_text, timestamp, status) in enumerate(rows, start=1):
        username_display = f"@{username}" if username else f"ID: {user_id}"
        status_display = "✅ پاسخ داده شده" if status == "replied" else "❗ بدون پاسخ"

        lines.append(
            f"<b>{idx}. کاربر:</b> {username_display}\n"
            f"📬 <b>پیام:</b> {message_text}\n"
            f"🕒 <b>زمان ارسال:</b> {timestamp.strftime('%H:%M %Y-%m-%d')}\n"
            f"{status_display}\n"
        )
        if status != "replied":
            reply_buttons.append(
                InlineKeyboardButton(f"✍️ پاسخ به شماره {idx}", callback_data=f"reply_to_{msg_id}")
            )

    text = "\n".join(lines)
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"unanswered_message_{page - 1}"
                                            ))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("➡️ بعدی", callback_data=f"unanswered_message_{page + 1}"
                                            ))
    reply_button_rows = [
        reply_buttons[i:i + 2] for i in range(0, len(reply_buttons), 2)
    ]
    reply_button_rows.insert(0, nav_row)
    reply_button_rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")])

    markup = InlineKeyboardMarkup(reply_button_rows)

    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)


async def handle_reply_to_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_reply_to_user_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    msg_id = callback["id"]
    set_timed_value(context, "reply_message_id", msg_id)
    set_user_state(context, STATE_AWAITING_ADMIN_REPLY)
    await query.message.reply_text("✍️ لطفاً پاسخ خود را برای این پیام ارسال کنید:")


async def handle_confirm_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = sanitize_text_input(update.message.text, max_len=1500, field_name="admin message")
    admin_user = update.effective_user
    msg_id = get_timed_value(context, "reply_message_id")
    if not msg_id:
        clear_user_state(context)
        await update.message.reply_text("â° Ù…Ù‡Ù„Øª Ù¾Ø§Ø³Ø®â€ŒÚ¯ÙˆÛŒÛŒ Ø¨Ù‡ Ù¾Ø§ÛŒØ§Ù† Ø±Ø³ÛŒØ¯.")
        return
    result = q_one("SELECT user_id FROM support_messages WHERE id = %s", (msg_id,))
    if not result:
        clear_timed_value(context, "reply_message_id")
        clear_user_state(context)
        await update.message.reply_text("âŒ Ù¾ÛŒØ§Ù… Ù…ÙˆØ±Ø¯Ù†Ø¸Ø± ÛŒØ§ÙØª Ù†Ø´Ø¯.")
        return
    user_userid = result[0]
    q_exec("""
        INSERT INTO admin_messages (support_message_id, admin_id, admin_username, reply_text)
        VALUES (%s, %s, %s, %s)
    """, (msg_id, admin_user.id, admin_user.username, text))
    q_exec("""
        UPDATE support_messages
        SET status = 'replied', replied_at = NOW()
        WHERE id = %s
    """, (msg_id,))
    await context.bot.send_message(
        chat_id=user_userid,
        text=f"📬 پاسخ ادمین به پیام شما:\n\n{text}"
    )
    await update.message.reply_text("✅ پاسخ شما ارسال شد.")
    await show_admin_panel(update, context)
    clear_timed_value(context, "reply_message_id")
    clear_user_state(context)


async def handle_search_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    buttons = [
        [InlineKeyboardButton('🔍 جستجو بر اساس یوزرنیم', callback_data="search_user_username")],
        [InlineKeyboardButton('🔢 جستجو بر اساس آیدی عددی', callback_data="search_user_userid")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_user_manage_menu")]
    ]

    message_text = "🔎 <b>یکی از روش‌های جستجو را انتخاب کنید:</b>"
    markup = InlineKeyboardMarkup(buttons)

    try:
        await query.edit_message_text(
            message_text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def handle_search_user_by_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_search_user_by_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    set_timed_value(context, "awaiting_find_user", True)
    callback = parse_callback(query.data)
    if callback.get("action") == "username":
        await query.edit_message_text("لطفاً یوزرنیم مورد نظر را وارد کنید:")
        set_timed_value(context, "search_by", "username")
    elif callback.get("action") == "userid":
        await query.edit_message_text("لطفاً آیدی عددی مورد نظر را وارد کنید:")
        set_timed_value(context, "search_by", "user_telegram_id")
    else:
        await query.edit_message_text("❌ گزینه نامعتبر است.")


async def handle_back_admin_panel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    await show_admin_panel(update, context)


async def handle_back_to_user_manage_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 نمایش لیست کاربران", callback_data="users_page_1")],
        [InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="search_user")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_admin_panel")]
    ])

    await query.edit_message_text(
        "👥 <b>مدیریت کاربران:</b>\nلطفاً یک گزینه را انتخاب کنید:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


async def handle_search_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    message_source = (
        update.message if update.message
        else update.callback_query.message if update.callback_query
        else None
    )
    if not message_source:
        return

    if not get_timed_value(context, "awaiting_find_user"):
        return
    search_by = get_timed_value(context, "search_by")
    if search_by == 'user_id':
        user_id = get_timed_value(context, "search_userid")
        user_detail = q_one("""
            SELECT id, user_id, name, username, phone, created_at, discount_percentage, 
                   freetrial, account_status, refcode, refered_by, cart_visibility
            FROM users WHERE id = %s
        """, (user_id,))

    elif search_by == "username":
        username = sanitize_text_input(update.message.text, max_len=64, field_name="username")
        user_detail = q_one("""
            SELECT id, user_id, name, username, phone, created_at, discount_percentage, 
                   freetrial, account_status, refcode, refered_by,  cart_visibility
            FROM users WHERE username = %s
        """, (username,))

    elif search_by == "user_telegram_id":
        telegram_id = sanitize_text_input(update.message.text, max_len=32, field_name="telegram id")
        user_detail = q_one("""
            SELECT id, user_id, name, username, phone, created_at, discount_percentage, 
                   freetrial, account_status, refcode, refered_by,  cart_visibility
            FROM users WHERE user_id = %s
        """, (telegram_id,))
    else:
        await message_source.reply_text("❌ روش جستجو نامعتبر است.")
        return

    if not user_detail:
        await message_source.reply_text("❗ کاربری با این مشخصات یافت نشد.")
        return

    (
        user_id, telegram_user_id, name, username, phone, created_at, discount,
        freetrial, status, refcode, refered_by, cart_visibility
    ) = user_detail
    invited_count = q_one("SELECT COUNT(*) FROM users WHERE refered_by = %s", (user_id,))[0]
    username_str = f"@{username}" if username else "بدون یوزرنیم"
    freetrial_str = "فعال" if freetrial else "غیرفعال"
    wallet = q_one("SELECT balance FROM wallets WHERE user_id = %s", (user_id,))
    wallet_balance = wallet[0] if wallet else 0
    transaction_count = q_one("SELECT COUNT(*) FROM wallet_transactions WHERE user_id = %s", (user_id,))[0]
    total_plans = q_one("SELECT COUNT(*) FROM purchased_plans WHERE user_id = %s", (user_id,))[0]
    active_count, inactive_count = q_one("""
        SELECT 
            COUNT(*) FILTER (WHERE is_active = TRUE),
            COUNT(*) FILTER (WHERE is_active = FALSE)
        FROM purchased_plans
        WHERE user_id = %s
    """, (user_id,))
    referrer_str = "ثبت نشده"
    if refered_by:
        ref_user = q_one("SELECT name, username FROM users WHERE user_id = %s", (refered_by,))
        if ref_user:
            ref_name, ref_username = ref_user
            if ref_username:
                referrer_str = f"@{ref_username}"
            elif ref_name:
                referrer_str = ref_name
    response = (
        f"👤 <b>اطلاعات کاربر:</b>\n"
        f"🆔 آیدی عددی: <code>{telegram_user_id}</code>\n"
        f"📛 نام: {name or 'نامشخص'}\n"
        f"📱 یوزرنیم: {username_str}\n"
        f"📞 تلفن: {phone or 'ثبت نشده'}\n"
        f"📆 عضویت: {created_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"🎁 درصد تخفیف: {discount}%\n"
        f"🆓 فری‌تریال: {freetrial_str}\n"
        f"🔐 وضعیت کاربر: {status}\n\n"
        f"💰 موجودی کیف پول: <b>{wallet_balance:,}</b> تومان\n"
        f"💸 تراکنش‌ها: <b>{transaction_count}</b> مورد\n"
        f"📦 تعداد خرید پلن: <b>{total_plans}</b>\n"
        f"✅ فعال: <b>{active_count}</b> | ❌ غیرفعال: <b>{inactive_count}</b>\n\n"
        f"📌 <b>کد معرفی:</b> <code>{refcode or 'ندارد'}</code>\n"
        f"👥 <b>تعداد دعوت موفق:</b> <b>{invited_count}</b>\n\n"
        f"🙋‍♂️ <b>معرف:</b> {referrer_str}\n"
        f"🛒 <b>نمایش شماره کارت:</b> {'✅ فعال' if cart_visibility else '❌ غیرفعال'}"
    )
    toggle_label = "⛔ غیرفعال‌کردن" if status == "active" else "✅ فعال‌کردن"
    buttons = [
        [
            InlineKeyboardButton('🎁 تغییر تخفیف', callback_data=f"user_discount_percentage_{user_id}"),
            InlineKeyboardButton(toggle_label, callback_data=f"user_in_active_{user_id}")
        ],
        [
            InlineKeyboardButton('🗑 حذف کاربر', callback_data=f"user_delete_{user_id}"),
            InlineKeyboardButton('💰 تغییر موجودی', callback_data=f"user_change_balance_{user_id}")
        ],
        [
            InlineKeyboardButton('📊 تراکنش‌ها', callback_data=f"user_transactions_{user_id}_1"),
            InlineKeyboardButton('📦 پلن‌های خریداری‌شده', callback_data=f"user_plans_{user_id}_1")
        ],
        [
            InlineKeyboardButton('✉️ ارسال پیام', callback_data=f"user_message_{user_id}"),
            InlineKeyboardButton('🔄 فعال/غیرفعال سازی نمایش شماره کارت', callback_data=f"user_cart_vis_{user_id}")
        ],
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_user_manage_menu")
        ]
    ]
    markup = InlineKeyboardMarkup(buttons)
    await message_source.reply_text(response, parse_mode="HTML", reply_markup=markup)
    clear_timed_value(context, "search_by")
    clear_timed_value(context, "search_userid")
    clear_timed_value(context, "awaiting_find_user")


async def handle_user_cart_vis_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    cart_vis_status = q_one("SELECT cart_visibility FROM users WHERE id = %s", (user_userid,))[0]
    if cart_vis_status:
        new_status = False
        msg = "🛒 نمایش کارت برای کاربر غیرفعال شد."
    else:
        new_status = True
        msg = "🛒 نمایش کارت برای کاربر فعال شد."
    q_exec("UPDATE users SET cart_visibility = %s WHERE id = %s", (new_status, user_userid))
    await query.edit_message_text(f"✅ {msg}")
    set_timed_value(context, "search_by", "user_id")
    set_timed_value(context, "search_userid", user_userid)
    set_timed_value(context, "awaiting_find_user", True)
    await handle_search_user(update, context)


async def handle_user_plans_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_user_plans_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()

    callback = parse_callback(query.data)
    user_id = callback["user_id"]
    page = callback["page"]

    PLANS_PER_PAGE = 5
    offset = (page - 1) * PLANS_PER_PAGE

    total = q_one("SELECT COUNT(*) FROM purchased_plans WHERE user_id = %s", (user_id,))[0]
    total_pages = (total + PLANS_PER_PAGE - 1) // PLANS_PER_PAGE

    rows = q_all("""
        SELECT id,
               config_data ->> 'remark' AS remark,
               expiry_date,
               is_active
        FROM purchased_plans
        WHERE user_id = %s
        ORDER BY purchase_date DESC
        LIMIT %s OFFSET %s
    """, (user_id, PLANS_PER_PAGE, offset))
    if not rows:
        await query.edit_message_text("❗ هیچ پلنی برای این کاربر یافت نشد.")
        return

    text = f"📦 <b>پلن‌های کاربر (صفحه {page}/{total_pages}):</b>\n\n"
    buttons = []
    for plan_id, remark, expiry, active in rows:
        active_str = "✅ فعال" if active else "❌ غیرفعال"
        text += f"🔹 <code>{remark}</code>\n📅 انقضا: {expiry.strftime('%Y-%m-%d')} | {active_str}\n\n"
        buttons.append([
            InlineKeyboardButton(f"👁 {plan_id}", callback_data=f"user_purchased_plan_{plan_id}"),
            InlineKeyboardButton("🔄 تمدید", callback_data=f"renewal_purchased_plan_{plan_id}"),
            InlineKeyboardButton("⚡ وضعیت", callback_data=f"dis_able_purchased_plan_{plan_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"delete_purchased_plan_{plan_id}")
        ])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"user_plans_{user_id}_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"user_plans_{user_id}_{page + 1}"))

    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"selected_user_{user_id}")])

    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)


TRANSACTIONS_PER_PAGE = 5


async def handle_user_transactions_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_user_transactions_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    user_userid = callback["user_id"]
    page = callback["page"]
    offset = (page - 1) * TRANSACTIONS_PER_PAGE
    total_transactions = q_one("SELECT COUNT(*) FROM wallet_transactions WHERE user_id = %s", (user_userid,))[0]
    total_pages = (total_transactions + TRANSACTIONS_PER_PAGE - 1) // TRANSACTIONS_PER_PAGE

    rows = q_all("""
        SELECT amount, type, method, description, status, created_at 
        FROM wallet_transactions 
        WHERE user_id = %s 
        ORDER BY created_at DESC 
        LIMIT %s OFFSET %s
    """, (user_userid, TRANSACTIONS_PER_PAGE, offset))

    if not rows:
        text = "هیچ تراکنشی برای این کاربر ثبت نشده است."
    else:
        lines = [f"📄 <b>تراکنش‌های کاربر (صفحه {page}/{total_pages}):</b>\n"]
        for amount, tx_type, method, desc, status, created_at in rows:
            lines.append(
                f"💳 مبلغ: {amount:,} تومان\n"
                f"📂 نوع: {'هزینه' if tx_type == 'spend' else 'افزایش'}"
                f"{' | روش: ' + method if method else ''}\n"
                f"📝 توضیح: {desc or 'ندارد'}\n"
                f"✅ وضعیت: {status}\n"
                f"🕒 تاریخ: {created_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"{'-' * 20}"
            )
        text = "\n".join(lines)
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"user_transactions_{user_userid}_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"user_transactions_{user_userid}_{page + 1}"))

    buttons = [nav_buttons] if nav_buttons else []
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"selected_user_{user_userid}")])
    markup = InlineKeyboardMarkup(buttons)

    await query.edit_message_text(text=text, parse_mode="HTML", reply_markup=markup)


async def handle_user_change_balance_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    set_timed_value(context, "user_userid", user_userid)
    set_timed_value(context, "awaiting_change_balance", True)
    await query.edit_message_text(
        "💰 لطفاً مقدار مورد نظر را وارد کنید.\n"
        "برای افزایش یا کاهش موجودی از علامت + یا - استفاده کنید.\n"
        "مثال:\n"
        "<code>+500</code> ➝ افزایش ۵۰۰ تومان\n"
        "<code>-200</code> ➝ کاهش ۲۰۰ تومان",
        parse_mode="HTML"
    )


async def handle_user_change_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.strip()
    if not get_timed_value(context, "awaiting_change_balance"):
        return
    user_userid = get_timed_value(context, "user_userid")
    if not user_userid:
        clear_timed_value(context, "awaiting_change_balance")
        await update.effective_message.reply_text("â° Ù…Ù‡Ù„Øª ØªØºÛŒÛŒØ± Ù…ÙˆØ¬ÙˆØ¯ÛŒ Ø¨Ù‡ Ù¾Ø§ÛŒØ§Ù† Ø±Ø³ÛŒØ¯.")
        return
    if text[0] not in ["-", "+"] or not text[1:].isdigit():
        await update.effective_message.reply_text("❌ فرمت وارد شده نادرست است. لطفاً عددی با علامت + یا - وارد کنید.")
        return
    change_amount = int(text)
    result = q_one("SELECT balance FROM wallets WHERE user_id = %s", (user_userid,))
    current_balance = result[0]
    new_balance = current_balance + change_amount
    if new_balance < 0:
        await update.effective_message.reply_text("❌ موجودی کافی نیست. نمی‌توان موجودی را منفی کرد.")
        return
    q_exec("""
        UPDATE wallets 
        SET balance = %s, updated_at = NOW() 
        WHERE user_id = %s
    """, (new_balance, user_userid))
    q_exec("""
        INSERT INTO wallet_transactions (
            user_id, amount, type, method, description, status, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW())
    """, (
        user_userid,
        abs(change_amount),
        "increase" if change_amount > 0 else "decrease",
        "admin_adjust",
        f"تغییر دستی موجودی توسط ادمین ({'+' if change_amount > 0 else '-'}{abs(change_amount)} تومان)",
        "success"
    ))
    await update.effective_message.reply_text(
        f"✅ موجودی کاربر با موفقیت بروزرسانی شد.\n"
        f"💳 موجودی جدید: <b>{new_balance:,} تومان</b>",
        parse_mode="HTML"
    )
    clear_timed_value(context, "user_userid")
    clear_timed_value(context, "awaiting_change_balance")
    set_timed_value(context, "search_by", "user_id")
    set_timed_value(context, "search_userid", user_userid)
    set_timed_value(context, "awaiting_find_user", True)
    await handle_search_user(update, context)


async def handle_user_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    msg = (
        "⚠️ آیا از حذف این کاربر اطمینان دارید؟\n"
        "با حذف کاربر، تمام کانفیگ‌ها و اطلاعات مرتبط نیز حذف خواهند شد.\n\n"
        "🔒 <b>توصیه:</b> در صورت نیاز به دسترسی مجدد، بهتر است کاربر را غیرفعال کنید."
    )
    buttons = [
        [InlineKeyboardButton("🗑 حذف کردن", callback_data=f"confirm_user_delete_{user_userid}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"selected_user_{user_userid}")]
    ]
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text=msg, parse_mode="HTML", reply_markup=markup)


async def handle_confirm_user_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    configs = q_all("""
        SELECT p.config_data, s.base_url, s.username, s.password
        FROM purchased_plans p
        JOIN server_plans sp ON p.plan_id = sp.id
        JOIN servers s ON sp.server_id = s.id
        WHERE p.user_id = %s
    """, (user_userid,))
    for config_data_json, base_url, server_username, server_password in configs:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        client_uuid = config_data.get("uuid")
        inbound_id = config_data.get("inbound_id")
        if not all([client_uuid, inbound_id, base_url]):
            continue
        try:
            await delete_panel_client(
                base_url=base_url,
                username=server_username,
                password=server_password,
                inbound_id=inbound_id,
                client_uuid=client_uuid,
            )
        except Exception as exc:
            print(f"❌ Failed to delete client {client_uuid}: {exc}")
            continue
    q_exec("DELETE FROM wallet_transactions WHERE user_id = %s", (user_userid,))
    q_exec("DELETE FROM wallets WHERE user_id = %s", (user_userid,))
    q_exec("DELETE FROM purchased_plans WHERE user_id = %s", (user_userid,))
    q_exec("DELETE FROM users WHERE id = %s", (user_userid,))

    await query.edit_message_text("✅ کاربر با موفقیت حذف شد و تمام کانفیگ‌های او از سرور نیز حذف شدند.")
    await handle_back_to_user_manage_menu(update, context)


async def handle_user_in_active_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    account_status = q_one("SELECT account_status FROM users WHERE id = %s", (user_userid,))[0]
    if account_status == "active":
        msg = "کاربر در حال حاضر فعال است. با یک کلیک همه پلن‌های او هم غیرفعال می‌شوند."
        buttons = [[InlineKeyboardButton("⛔ غیرفعال‌کردن فوری", callback_data=f"confirm_user_in_active_{user_userid}")]]
    else:
        msg = "کاربر در حال حاضر غیرفعال است. با یک کلیک همه پلن‌های او هم فعال می‌شوند."
        buttons = [[InlineKeyboardButton("✅ فعال‌کردن فوری", callback_data=f"confirm_user_active_{user_userid}")]]
    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data=f"selected_user_{user_userid}")])
    markup = InlineKeyboardMarkup(buttons)
    await query.edit_message_text(text=msg, reply_markup=markup)


async def handle_confirm_user_in_active_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    data = query.data
    user_userid = int(data.rsplit("_", 1)[-1])
    activate = "in_" not in data
    new_status = "active" if activate else "inactive"
    new_plan_state = True if activate else False
    q_exec("UPDATE users SET account_status = %s WHERE id = %s", (new_status, user_userid))
    q_exec("UPDATE purchased_plans SET is_active = %s WHERE user_id = %s", (new_plan_state, user_userid))
    configs = q_all("""
        SELECT p.expiry_date, p.max_traffic_gb, p.config_data,
               s.base_url, s.username, s.password
        FROM purchased_plans p
        JOIN server_plans sp ON p.plan_id = sp.id
        JOIN servers s ON sp.server_id = s.id
        WHERE p.user_id = %s
    """, (user_userid,))
    for expiry_date, max_traffic_gb, config_data_json, base_url, server_username, server_password in configs:
        config_data = json.loads(config_data_json) if isinstance(config_data_json, str) else config_data_json
        client_uuid = config_data.get("uuid")
        inbound_id = config_data.get("inbound_id")
        raw_url = config_data.get("connection_url", "")
        email = unquote(raw_url.split("#")[-1]) if "#" in raw_url else client_uuid

        if not all([client_uuid, inbound_id, base_url]):
            continue

        try:
            await update_panel_client(
                base_url=base_url,
                username=server_username,
                password=server_password,
                inbound_id=inbound_id,
                client_uuid=client_uuid,
                email=email,
                expiry_date=expiry_date,
                max_gb=max_traffic_gb,
                enable=activate,
            )
        except Exception as exc:
            print(f"❌ Update failed for client {client_uuid}: {exc}")
            continue
    await query.edit_message_text(
        f"✅ وضعیت کاربر به <b>{'فعال' if activate else 'غیرفعال'}</b> تغییر یافت.\n"
        f"{'تمام کانفیگ‌ها فعال شدند.' if activate else 'تمام کانفیگ‌ها غیرفعال شدند.'}",
        parse_mode="HTML"
    )
    set_timed_value(context, "search_by", "user_id")
    set_timed_value(context, "search_userid", user_userid)
    set_timed_value(context, "awaiting_find_user", True)
    clear_timed_value(context, "user_userid")
    clear_timed_value(context, "awaiting_change_discount")
    await handle_search_user(update, context)


async def handle_user_discount_percentage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_userid = int(query.data.rsplit("_", 1)[-1])
    set_timed_value(context, "user_userid", user_userid)
    set_timed_value(context, "awaiting_change_discount", True)
    await query.edit_message_text("مقدار تخفیف مورد نظر را وارد کنید: (به صورت عددی مثلا 5، به معنی 5%) ")


async def handle_user_discount_percentage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = sanitize_text_input(update.message.text, max_len=8, field_name="discount")
    if not get_timed_value(context, "awaiting_change_discount"):
        return
    user_userid = get_timed_value(context, "user_userid")
    if not user_userid:
        clear_timed_value(context, "awaiting_change_discount")
        await update.message.reply_text("â° Ù…Ù‡Ù„Øª ØªØºÛŒÛŒØ± ØªØ®ÙÛŒÙ Ø¨Ù‡ Ù¾Ø§ÛŒØ§Ù† Ø±Ø³ÛŒØ¯.")
        return

    try:
        discount = int(text)
        if not (0 <= discount <= 100):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ لطفاً یک عدد بین 0 تا 100 وارد کنید.")
        return

    q_exec("""
        UPDATE users 
        SET discount_percentage = %s 
        WHERE id = %s
    """, (discount, user_userid))
    await update.message.reply_text(f"✅ مقدار تخفیف با موفقیت به {discount}% تغییر یافت.")
    set_timed_value(context, "search_by", "user_id")
    set_timed_value(context, "search_userid", user_userid)
    set_timed_value(context, "awaiting_find_user", True)
    clear_timed_value(context, "user_userid")
    clear_timed_value(context, "awaiting_change_discount")
    await handle_search_user(update, context)


USERS_PER_PAGE = 10


async def show_users(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1):
    offset = (page - 1) * USERS_PER_PAGE
    total_users = q_one("SELECT COUNT(*) FROM users")[0]
    total_pages = (total_users + USERS_PER_PAGE - 1) // USERS_PER_PAGE

    users = q_all("""
        SELECT id, name, username, user_id
        FROM users
        ORDER BY created_at ASC
        LIMIT %s OFFSET %s
    """, (USERS_PER_PAGE, offset))

    buttons = []
    for user_id, name, username, telegram_user_id in users:
        display_name = name or f"ID:{telegram_user_id}"
        display_username = f"@{username}" if username else "بدون یوزرنیم"
        label = f"{display_name} | {display_username}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"selected_user_{user_id}")])

    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"users_page_{page - 1}"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"users_page_{page + 1}"))
    if nav_buttons:
        buttons.append(nav_buttons)

    buttons.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_user_manage_menu")])

    markup = InlineKeyboardMarkup(buttons)

    message_text = (
        f"👥 <b>لیست کاربران (صفحه {page}/{total_pages}):</b>\n"
        f"برای مشاهده جزئیات یکی را انتخاب کنید:"
    )

    if update.message:
        await update.message.reply_text(
            message_text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    elif update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                message_text,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except telegram.error.BadRequest as e:
            if "Message to edit not found" in str(e):
                await update.callback_query.message.reply_text(
                    message_text,
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            else:
                raise


async def handle_selected_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_selected_user_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    user_userid = callback["id"]
    set_timed_value(context, "awaiting_find_user", True)
    set_timed_value(context, "search_by", "user_id")
    set_timed_value(context, "search_userid", user_userid)
    try:
        await query.message.delete()
    except Exception:
        pass
    await handle_search_user(update, context)


async def handle_users_pagination_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    logger.info("handle_users_pagination_callback admin=%s", getattr(update.effective_user, "id", None))
    query = update.callback_query
    await query.answer()
    callback = parse_callback(query.data)
    page = callback["id"]
    await query.message.delete()
    await show_users(update, context, page=page)


async def handle_return_my_profile_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.delete_message()
    except Exception:
        pass

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="↩️ به منوی اصلی برگشتید."
    )


async def handle_return_main_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    callback_data = query.data

    if callback_data == "return_main_menu":
        old_messages = context.user_data.get("delete_after_return", [])
        for msg_id in old_messages:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except:
                pass
        context.user_data.pop("delete_after_return", None)

        await main_menu(update, context)


async def handle_return_buy_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    callback_data = query.data
    if callback_data == "return_buy_server":
        old_messages = context.user_data.get("delete_after_return", [])
        for msg_id in old_messages:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except:
                pass
        context.user_data.pop("delete_after_return", None)
        await show_buy_server_panel(update, context)


async def handle_return_setup_server_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    callback_data = query.data
    if callback_data == "return_setup_server_menu":
        old_messages = context.user_data.get("delete_after_return", [])
        for msg_id in old_messages:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except:
                pass
        context.user_data.pop("delete_after_return", None)
        set_user_state(context, STATE_BACK_SETUP_SERVERS)
        buttons = [
            [KeyboardButton("➕ افزودن سرور"), KeyboardButton("📊 نمایش سرورها")],
            [KeyboardButton('⚙️ تنظیمات سرور تست'), KeyboardButton("📦 تعیین طرح‌های فروش")],
            [KeyboardButton("↩️ بازگشت")]
        ]
        markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
        await query.message.reply_text("لطفاً یکی از گزینه‌ها را انتخاب کنید: 🔽", reply_markup=markup)
        return


async def handle_category_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.strip()
    step_data = get_timed_value(context, "add_category")

    if not step_data:
        return

    if step_data["step"] == "name":
        step_data["name"] = sanitize_text_input(text, max_len=64, field_name="category name")
        step_data["step"] = "emoji"
        set_timed_value(context, "add_category", step_data)
        await update.message.reply_text(
            text="""
        🔤 <b>انتخاب ایموجی</b>

        لطفاً یک ایموجی برای این دسته‌بندی وارد کنید.

        ✅ این گزینه اختیاری است. اگر نمی‌خواهید ایموجی انتخاب کنید، کلمه <code>skip</code> را ارسال نمایید.
            """,
            parse_mode="HTML"
        )

        return

    elif step_data["step"] == "emoji":
        if text == "skip":
            emoji = " "
        else:
            emoji = sanitize_text_input(text, max_len=8, field_name="category emoji")
        name = step_data["name"]

        q_exec("INSERT INTO categories (name, emoji) VALUES (%s, %s)", (name, emoji))

        await update.message.reply_text(f"✅ دسته‌بندی <b>{name}</b> با موفقیت اضافه شد.", parse_mode="HTML")
        clear_timed_value(context, "add_category")
        await show_categories_menu(update, context)
        return


async def handle_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    callback_data = query.data
    if callback_data == "return_servers":
        for msg_id in context.user_data.get("delete_after_return", []):
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except telegram.error.BadRequest:
                pass
        server_list = q_all("SELECT name FROM servers ORDER BY created_at ASC")
        if not server_list:
            await query.message.reply_text("❌ هیچ سروری یافت نشد.")
            return

        keyboard = []
        for i, row in enumerate(server_list):
            server_name = row[0]
            keyboard.append([InlineKeyboardButton(server_name, callback_data=f"server_{i}")])
        keyboard.append([InlineKeyboardButton('بازگشت به منوی اصلی', callback_data="return_setup_server_menu")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        msg = await query.message.reply_text("یکی از سرورها را انتخاب کنید: 🔽", reply_markup=reply_markup)
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    try:
        prefix, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
    except (ValueError, IndexError):
        return

    servers = q_all("""
        SELECT name, base_url, username, password, location, is_active,
               max_traffic_gb, used_traffic_gb, current_users, max_users, created_at
        FROM servers ORDER BY created_at ASC
    """)

    if index >= len(servers):
        await query.edit_message_text("❗️سرور مورد نظر پیدا نشد.")
        return

    server = servers[index]
    name, base_url, username, password, location, is_active, max_traffic, used_traffic, current_users, max_users, created_at = server
    if callback_data.startswith("delete_server_"):
        q_exec("DELETE FROM servers WHERE name = %s", (name,))
        await query.message.reply_text(f"✅ سرور «{name}» با موفقیت حذف شد.")
        return

    keyboard = [
        [InlineKeyboardButton('📝 ادیت کردن سرور', callback_data=f"edit_server_{index}")],
        [InlineKeyboardButton("❌ حذف کردن سرور", callback_data=f"delete_server_{index}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_servers")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = f"""
    📡 <b>اطلاعات سرور:</b>
    🔹 <b>نام:</b> {name}
    🌐 <b>Base URL:</b> <code>{base_url}</code>
    👤 <b>نام کاربری:</b> <code>{username}</code>
    🔑 <b>رمز عبور:</b> <code>{password}</code>
    📍 <b>موقعیت:</b> {location}
    📶 <b>فعال:</b> {"✅ بله" if is_active else "❌ خیر"}
    📊 <b>ترافیک مصرف‌شده:</b> {used_traffic:.2f} GB / {max_traffic:.2f} GB
    👥 <b>کاربران:</b> {current_users} / {max_users}
    🗓️ <b>تاریخ ساخت:</b> {created_at.strftime('%Y-%m-%d %H:%M')}
    """
    try:
        masag = await query.edit_message_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)
        context.user_data.setdefault("delete_after_return", []).append(masag.message_id)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def handle_test_config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()

    if query.data == "delete_test_config":
        q_exec("DELETE FROM test_server_config")
        clear_timed_value(context, "test_config")
        await query.edit_message_text("✅ تنظیمات سرور تست حذف شد.")
        return

    elif query.data == "edit_test_config":
        existing = q_one("""
            SELECT t.is_active, t.server_id, s.name, t.inbound_id, t.traffic_gb, t.duration_days
            FROM test_server_config t
            LEFT JOIN servers s ON t.server_id = s.id
            LIMIT 1
        """)
        draft = {"step": "is_active"}
        if existing:
            is_active, server_id, server_name, inbound_id, traffic_gb, duration_days = existing
            draft.update({
                "is_active": bool(is_active),
                "server_id": server_id,
                "server_name": server_name,
                "inbound_id": inbound_id,
                "traffic_gb": traffic_gb,
                "duration_days": duration_days,
            })
        set_timed_value(context, "test_config", draft)
        await query.edit_message_text(
            text="""
        🔧 <b>فعال‌سازی سرور تست</b>

        آیا می‌خواهید سرور تست فعال باشد؟  
        ➖ لطفاً یکی از گزینه‌های <b>بله</b> یا <b>خیر</b> را انتخاب کنید.
            """,
            parse_mode="HTML"
        )
        return


async def handle_server_plans_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "add_category":
        set_timed_value(context, "add_category", {"step": "name"})
        await query.message.reply_text(
            text="""
        📝 <b>افزودن نام دسته‌بندی</b>

        لطفاً نام مورد نظر برای این دسته‌بندی را وارد کنید.
            """,
            parse_mode="HTML"
        )

        return

    if query.data.startswith("admin_view_category_"):
        for msg_id in context.user_data.get("delete_after_return", []):
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except telegram.error.BadRequest:
                pass
        cat_id = int(query.data.split("_")[-1])
        context.user_data["current_category_id"] = cat_id
        server_plans = q_all("""
                SELECT p.id, p.traffic_gb, p.duration_days, p.price, s.name
                FROM server_plans p
                JOIN servers s ON p.server_id = s.id
                WHERE p.category_id = %s
                ORDER BY p.duration_days ASC, p.traffic_gb ASC
            """, (cat_id,))
        buttons = []

        if server_plans:
            for plan in server_plans:
                plan_id, traffic_gb, duration_days, price, name = plan
                label = f"💠 {traffic_gb} گیگ ⏳ {duration_days} روزه 💳 {price} تومان"
                buttons.append([
                    InlineKeyboardButton(label, callback_data=f"admin_view_plan_{plan_id}")
                ])

            message_text = (
                "<b>📦 طرح‌های فروش موجود:</b>\n\n"
                "برای مشاهده یا ویرایش، یکی از طرح‌ها را انتخاب کنید."
            )
        else:
            message_text = "<b>❗️هیچ طرحی موجود نیست.</b>"

        buttons.extend([
            [InlineKeyboardButton("➕ افزودن طرح", callback_data="add_plan_in_category")],
            [
                InlineKeyboardButton("📝 ویرایش دسته‌بندی", callback_data=f"admin_edit_category_{cat_id}"),
                InlineKeyboardButton("❌ حذف دسته بندی", callback_data=f"admin_delete_category_{cat_id}")
            ],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_categories")]
        ])

        keyboard = InlineKeyboardMarkup(buttons)

        msg = await query.message.reply_text(
            message_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )
        context.user_data.setdefault("delete_after_return", []).append(msg.message_id)
    elif query.data == "back_to_categories":
        await show_categories_menu(update, context)
        return

    if query.data == "add_plan_in_category":
        set_timed_value(context, "server_plans", {
            "step": "traffic_gb",
            "category_id": context.user_data.get("current_category_id")
        })
        await query.message.reply_text("🔢 مقدار ترافیک (گیگابایت) را وارد کنید:", reply_markup=ReplyKeyboardRemove())
        return


async def show_categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    for msg_id in context.user_data.get("delete_after_return", []):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except telegram.error.BadRequest:
            pass
    context.user_data["delete_after_return"].clear()
    for msg_id in context.user_data.get("delete_return", []):
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except telegram.error.BadRequest:
            pass
    context.user_data["delete_after_return"].clear()

    categories = q_all("SELECT id, name, emoji FROM categories")
    buttons = []
    if categories:
        for cat_id, name, emoji in categories:
            buttons.append([
                InlineKeyboardButton(f"{name} {emoji}", callback_data=f"admin_view_category_{cat_id}")
            ])
    buttons.append([InlineKeyboardButton("➕ افزودن دسته‌بندی", callback_data="add_category")])
    buttons.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="return_setup_server_menu")])

    keyboard = InlineKeyboardMarkup(buttons)
    if update.message:
        msg = await update.message.reply_text(
            "<b>📦 دسته‌بندی‌های طرح‌های فروش:</b>\n\n"
            "لطفاً یکی از دسته‌ها را انتخاب کنید یا دسته‌ای جدید اضافه نمایید.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    else:
        msg = await update.callback_query.message.reply_text(
            "<b>📦 دسته‌بندی‌های طرح‌های فروش:</b>\n\n"
            "لطفاً یکی از دسته‌ها را انتخاب کنید یا دسته‌ای جدید اضافه نمایید.",
            parse_mode="HTML",
            reply_markup=keyboard
        )
    context.user_data["delete_after_return"].append(msg.message_id)


async def handle_view_plans_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user_id = query.from_user.id
    try:
        _, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
    except (ValueError, IndexError):
        await query.message.reply_text("❗️فرمت کال‌بک نامعتبر است.")
        return
    if callback_data.startswith("delete_plan_"):
        q_exec("DELETE FROM server_plans WHERE id = %s", (index,))
        context.user_data.pop("edit_plan", None)
        await query.edit_message_text("🗑 طرح با موفقیت حذف شد.")
        await show_categories_menu(update, context)
        return

    plan_row = q_one(
        """
        SELECT 
            sp.id, sp.traffic_gb, sp.duration_days, sp.price, sp.created_at,
            sp.inbound_id, sp.is_active,
            c.name AS category_name,
            s.name AS server_name
        FROM server_plans sp
        JOIN categories c ON sp.category_id = c.id
        JOIN servers s ON sp.server_id = s.id
        WHERE sp.id = %s
        """,
        (index,)
    )
    (plan_id, traffic, duration, price, created_at, inbound_id, is_active, category_name,
     server_name) = plan_row
    context.user_data["edit_plan"] = {
        "id": plan_id,
        "traffic_gb": traffic,
        "duration_days": duration,
        "price": price,
        "inbound_id": inbound_id,
        "is_active": is_active,
        "category_name": category_name,
        "server_name": server_name
    }
    status_emoji = "✅ فعال" if is_active else "❌ غیرفعال"
    message = f"""
    📦 <b>جزئیات طرح</b>

    🚦 <b>وضعیت:</b> {status_emoji}

    📂 <b>دسته‌بندی:</b> <code>{category_name}</code>
    🖥 <b>سرور:</b> <code>{server_name}</code>

    📶 <b>حجم ترافیک:</b> <code>{traffic} گیگابایت</code>
    ⏳ <b>مدت اعتبار:</b> <code>{duration} روز</code>
    💰 <b>قیمت:</b> <code>{price} تومان</code>

    🛠 <b>شناسه Inbound:</b> <code>{inbound_id}</code>
    📅 <b>تاریخ ایجاد:</b> <code>{created_at.strftime('%Y-%m-%d')}</code>
    """

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ ویرایش", callback_data=f"edit_plan_{plan_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"delete_plan_{plan_id}")
        ],
        [InlineKeyboardButton("🔙 بازگشت به دسته بندی ها", callback_data="back_to_categories")]
    ])

    await query.edit_message_text(
        text=message,
        parse_mode="HTML",
        reply_markup=keyboard
    )


async def handle_edit_plan_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    await show_edit_plan_server_menu(update, context, query.message)


async def show_edit_plan_server_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, message):
    if not is_admin(update.effective_user.id):
        return
    plan_id = context.user_data["edit_plan"]["id"]
    plan_row = q_one(
        """
        SELECT 
            sp.id, sp.traffic_gb, sp.duration_days, sp.price, sp.created_at,
            sp.inbound_id, sp.is_active,
            c.name AS category_name,
            s.name AS server_name
        FROM server_plans sp
        JOIN categories c ON sp.category_id = c.id
        JOIN servers s ON sp.server_id = s.id
        WHERE sp.id = %s
        """,
        (plan_id,)
    )
    (plan_id, traffic, duration, price, created_at, inbound_id, is_active, category_name,
     server_name) = plan_row
    text = (
        "✏️ <b>ویرایش طرح فروش</b>\n\n"
        "یکی از گزینه‌های زیر را برای ویرایش انتخاب کنید:"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💰 قیمت: {price} تومان", callback_data="edit_plan_price")],
        [InlineKeyboardButton(f"🛠 Inbound ID: {inbound_id}", callback_data="edit_plan_inbound_id")],
        [InlineKeyboardButton(f"📶 ترافیک: {traffic} GB", callback_data="edit_plan_traffic_gb")],
        [InlineKeyboardButton(f"⏳ مدت: {duration} روز", callback_data="edit_plan_duration_days")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_categories")]
    ])
    try:
        msg = await message.edit_text(text=text, parse_mode="HTML", reply_markup=keyboard)
        context.user_data.setdefault("delete_return", []).append(msg.message_id)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def handle_edit_field_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    plan = context.user_data["edit_plan"]
    field = callback_data.split("_", 2)[-1]
    msg = f"📝 مقدار فعلی {field}: <code>{plan[field]}</code>\n\n" \
          f"✏️ لطفاً مقدار جدید را وارد کنید یا بازگشت را انتخاب کنید."
    keyboard = [
        [InlineKeyboardButton("✅ تایید", callback_data=f"confirm_edit_plan_{field}_{plan['id']}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"edit_plan_{plan['id']}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)
    set_user_state(context, STATE_AWAITING_EDIT_PLAN_VALUE)


async def handle_edit_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    user_id = query.from_user.id
    try:
        _, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
    except (ValueError, IndexError):
        await query.message.reply_text("❗️فرمت کال‌بک نامعتبر است.")
        return
    if callback_data.startswith('admin_delete_category_'):
        q_exec("DELETE FROM categories WHERE id = %s", (index,))

        await query.message.reply_text("✅ دسته‌بندی با موفقیت حذف شد.")
        await show_categories_menu(update, context)
    if callback_data.startswith("admin_edit_category_"):
        result = q_one("SELECT name, emoji FROM categories WHERE id = %s", (index,))
        if not result:
            await query.message.reply_text("❗️دسته‌بندی مورد نظر یافت نشد.")
            return
        current_name = result[0]
        current_emoji = result[1]
        context.user_data["edit_category"] = {"step": "name"}
        context.user_data["edit_category"]["id"] = index
        context.user_data["edit_category"]["name"] = current_name
        context.user_data["edit_category"]["emoji"] = current_emoji
        await query.edit_message_text(
            text=f"""
    📝 <b>ویرایش دسته‌بندی</b>

    📌 <b>نام فعلی:</b> <i>{current_name}</i>

    ✏️ اگر مایل به تغییر نام هستید، لطفاً نام جدید را وارد کنید.
    ⏭ در غیر این صورت، کلمه <code>skip</code> را ارسال نمایید.
            """,
            parse_mode="HTML"
        )
        return


async def handle_add_server_plan_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = update.message.text.strip()
    data = get_timed_value(context, "server_plans", {}) or {}
    step = data.get("step")

    if step == "traffic_gb":
        try:
            data["traffic_gb"] = int(text)
        except ValueError:
            await update.message.reply_text("❗️لطفاً فقط عدد وارد کنید.")
            return
        data["step"] = "duration_days"
        set_timed_value(context, "server_plans", data)
        await update.message.reply_text("🕐 تعداد روزها را وارد کنید:")

    elif step == "duration_days":
        try:
            data["duration_days"] = int(text)
        except ValueError:
            await update.message.reply_text("❗️عدد معتبر وارد کنید.")
            return
        data["step"] = "price"
        set_timed_value(context, "server_plans", data)
        await update.message.reply_text("💵 قیمت را ارسال کنید:")

    elif step == "price":
        try:
            data["price"] = int(text)
        except ValueError:
            await update.message.reply_text("❗️فقط عدد وارد شود.")
            return
        data["step"] = "server_name"
        set_timed_value(context, "server_plans", data)
        await update.message.reply_text("🌍 نام سرور را وارد کنید:")

    elif step == "server_name":
        text = sanitize_text_input(text, max_len=128, field_name="server name")
        server = q_one("SELECT id FROM servers WHERE name = %s", (text,))
        if not server:
            await update.message.reply_text("❗️سروری با این نام پیدا نشد. لطفاً نام صحیح سرور را دوباره وارد کنید:")
            return
        data["server_name"] = text
        data["server_id"] = server[0]
        data["step"] = "inbound_id"
        set_timed_value(context, "server_plans", data)
        await update.message.reply_text("🔢 عدد اینباند را وارد کنید:")
        return

    elif step == "inbound_id":
        try:
            data["inbound_id"] = int(text)
        except ValueError:
            await update.message.reply_text("❗️اینباند باید عدد باشد.")
            return

        q_exec(
            """
            INSERT INTO server_plans 
            (traffic_gb, duration_days, price, server_id, inbound_id, category_id, created_at, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, NOW(), TRUE)
            """,
            (
                data["traffic_gb"],
                data["duration_days"],
                data["price"],
                data["server_id"],
                data["inbound_id"],
                data["category_id"]
            )
        )

        await update.message.reply_text(
            f"✅ طرح با موفقیت اضافه شد:\n"
            f"📦 حجم: {data['traffic_gb']} گیگ\n"
            f"🕐 مدت: {data['duration_days']} روز\n"
            f"💳 قیمت: {data['price']} تومان\n"
            f"🌍 سرور: {data['server_name']}\n"
            f"🔢 اینباند: {data['inbound_id']}"
        )
        set_user_state(context, STATE_BACK_SETUP_SERVERS)
        clear_timed_value(context, "server_plans")
        await show_categories_menu(update, context)

    else:
        await update.message.reply_text("❗️مشکلی در مراحل وجود دارد. دوباره تلاش کنید.")


async def show_edit_server_menu(index: int, context: ContextTypes.DEFAULT_TYPE, message):
    servers = q_all("""
        SELECT name, base_url, username, password, location, is_active,
               max_traffic_gb, used_traffic_gb, current_users, max_users, created_at
        FROM servers ORDER BY created_at ASC
    """)

    server = servers[index]
    (
        name, base_url, username, password, location, is_active,
        max_traffic, used_traffic, current_users, max_users, created_at
    ) = server

    keyboard = [
        [InlineKeyboardButton(f'🔹 نام سرور: {name}', callback_data=f"callback_edit_server_name_{index}")],
        [InlineKeyboardButton(f"🌐 Base URL: {base_url}", callback_data=f"callback_edit_server_url_{index}")],
        [InlineKeyboardButton(f"👤 نام کاربری: {username}", callback_data=f"callback_edit_server_username_{index}")],
        [InlineKeyboardButton(f"🔑 رمز عبور: {password}", callback_data=f"callback_edit_server_password_{index}")],
        [InlineKeyboardButton(f"📍 موقعیت: {location}", callback_data=f"callback_edit_server_loc_{index}")],
        [InlineKeyboardButton(f"{'✅ فعال' if is_active else '❌ غیرفعال'}",
                              callback_data=f"callback_edit_server_active_{index}")],
        [InlineKeyboardButton(f"💾 حداکثر ترافیک: {max_traffic} GB",
                              callback_data=f"callback_edit_server_maxgb_{index}")],
        [InlineKeyboardButton(f"👥 حداکثر کاربران: {max_users}", callback_data=f"callback_edit_server_users_{index}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="return_servers")]
    ]
    msg = """
🛠 <b>ویرایش اطلاعات سرور</b>

🔍 <i>اطلاعات فعلی سرور به شرح زیر است:</i>

📌 <b>برای ویرایش هر بخش، روی گزینه مورد نظر کلیک کنید.</b>
    """
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await message.edit_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            pass
        else:
            raise


async def handle_edit_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    try:
        _, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
    except (ValueError, IndexError):
        await query.message.reply_text("❗️فرمت کال‌بک نامعتبر است.")
        return
    await show_edit_server_menu(index, context, query.message)


async def handle_edit_server_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    callback_data = query.data
    try:
        _, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
    except (ValueError, IndexError):
        await query.message.reply_text("❗️فرمت کال‌بک نامعتبر است.")
        return
    servers = q_all("""
            SELECT name, base_url, username, password, location, is_active,
                   max_traffic_gb, used_traffic_gb, current_users, max_users, created_at
            FROM servers ORDER BY created_at ASC
        """)

    server = servers[index]
    (
        name, base_url, username, password, location, is_active,
        max_traffic, used_traffic, current_users, max_users, created_at
    ) = server
    context.user_data["edit_server"] = {
        "index": index,
        "name": name,
        "base_url": base_url,
        "username": username,
        "password": password,
        "location": location,
        "is_active": is_active,
        "max_traffic_gb": max_traffic,
        "used_traffic": used_traffic,
        "current_users": current_users,
        "max_users": max_users,
        "created_at": created_at,
        "step": "base_url"
    }
    if callback_data.startswith("callback_edit_server_"):
        base, index_str = callback_data.rsplit("_", 1)
        index = int(index_str)
        raw_field = base.removeprefix("callback_edit_server_")
        field = FIELD_MAP.get(raw_field)
        context.user_data["current_editing_field"] = field
        current_val = context.user_data["edit_server"][field]
        msg = f"📝 مقدار فعلی {field}: <code>{current_val}</code>\n\n" \
              f"✏️ لطفاً مقدار جدید را وارد کنید یا بازگشت را انتخاب کنید."
        keyboard = [
            [InlineKeyboardButton("✅ تایید", callback_data=f"confirm_edit_{field}_{index}")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data=f"edit_server_{index}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text=msg, parse_mode="HTML", reply_markup=reply_markup)
        set_user_state(context, STATE_AWAITING_EDIT_SERVER_VALUE)


async def handle_confirm_edit_server_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    if get_user_state(context) != STATE_AWAITING_EDIT_SERVER_VALUE:
        await query.message.reply_text("❗️وضعیت ویرایش منقضی شده یا معتبر نیست.")
        return
    user_id = query.from_user.id
    field = context.user_data.get("current_editing_field")
    edit_data = context.user_data.get("edit_server")
    if not field or not edit_data or field not in ALLOWED_SERVER_EDIT_FIELDS:
        await query.message.reply_text("❌ فیلد درخواستی معتبر نیست.")
        return

    new_value = context.user_data.get("new_value")
    if new_value is None:
        msg = await query.message.reply_text("❗️لطفاً ابتدا مقدار جدید را وارد کنید.")
        context.user_data.setdefault("bot_messages", []).append(msg.message_id)
        return

    try:
        new_value = sanitize_server_field_value(field, new_value)
    except ValueError as e:
        await query.message.reply_text(f"❌ {e}")
        return

    try:
        q_exec(f"""
            UPDATE servers
            SET {field} = %s
            WHERE name = %s
        """, (new_value, edit_data["name"]))
    except Exception:
        logger.exception("handle_confirm_edit_server_callback db error")
        await query.message.reply_text("⚠️ خطا هنگام ذخیره در پایگاه داده.")
        return
    old_messages = context.user_data.get("bot_messages", [])
    for msg_id in old_messages:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass
    context.user_data.pop("bot_messages", None)
    await query.message.reply_text(f"✅ فیلد <b>{field}</b> با موفقیت بروزرسانی شد.", parse_mode="HTML")
    await show_edit_server_menu(edit_data["index"], context, query.message)
    context.user_data.pop("new_value", None)
    context.user_data.pop("current_editing_field", None)
    clear_user_state(context)


async def handle_confirm_edit_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    query = update.callback_query
    await query.answer()
    if get_user_state(context) != STATE_AWAITING_EDIT_PLAN_VALUE:
        await query.message.reply_text("❗️وضعیت ویرایش منقضی شده یا معتبر نیست.")
        return
    plan = context.user_data.get("edit_plan")
    field = context.user_data.get("current_editing_field")
    if not plan or not field or field not in ALLOWED_PLAN_EDIT_FIELDS:
        await query.message.reply_text("❌ فیلد درخواستی معتبر نیست.")
        return
    user_id = query.from_user.id
    new_value = context.user_data.get("new_value")
    if new_value is None:
        msg = await query.message.reply_text("❗️لطفاً ابتدا مقدار جدید را وارد کنید.")
        context.user_data.setdefault("bot_messages", []).append(msg.message_id)
        return
    try:
        new_value = sanitize_plan_field_value(field, new_value)
    except ValueError as e:
        await query.message.reply_text(f"❌ {e}")
        return
    try:
        q_exec(f"""
            UPDATE server_plans
            SET {field} = %s
            WHERE id = %s
        """, (new_value, plan["id"]))
    except Exception:
        logger.exception("handle_confirm_edit_plan_callback db error")
        await query.message.reply_text("⚠️ خطا هنگام ذخیره در پایگاه داده.")
        return
    old_messages = context.user_data.get("bot_messages", [])
    for msg_id in old_messages:
        try:
            await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass
    context.user_data.pop("bot_messages", None)
    await query.message.reply_text(f"✅ فیلد <b>{field}</b> با موفقیت بروزرسانی شد.", parse_mode="HTML")
    try:
        await query.message.delete()
    except Exception:
        pass
    context.user_data.pop("new_value", None)
    context.user_data.pop("current_editing_field", None)
    clear_user_state(context)
    await asyncio.sleep(0.5)
    msg = await query.message.reply_text("⏳ بارگذاری منوی ویرایش جدید...", parse_mode="HTML")
    await show_edit_plan_server_menu(update, context, msg)


async def handle_text_input_edit_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    state = get_user_state(context)
    if state != STATE_AWAITING_EDIT_SERVER_VALUE:
        return
    field = context.user_data.get("current_editing_field")
    if field not in ALLOWED_SERVER_EDIT_FIELDS:
        await update.message.reply_text("❌ فیلد قابل ویرایش معتبر نیست.")
        return
    text = sanitize_text_input(update.message.text, max_len=256, field_name=field)
    try:
        context.user_data["new_value"] = sanitize_server_field_value(field, text)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    msg = await update.message.reply_text("✅ مقدار جدید دریافت شد. برای تایید، روی دکمه تایید کلیک کنید.")
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)


async def handle_text_input_edit_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    state = get_user_state(context)
    if state != STATE_AWAITING_EDIT_PLAN_VALUE:
        return
    field = context.user_data.get("current_editing_field")
    if field not in ALLOWED_PLAN_EDIT_FIELDS:
        await update.message.reply_text("❌ فیلد قابل ویرایش معتبر نیست.")
        return
    text = sanitize_text_input(update.message.text, max_len=128, field_name=field)
    try:
        context.user_data["new_value"] = sanitize_plan_field_value(field, text)
    except ValueError as e:
        await update.message.reply_text(f"❌ {e}")
        return
    msg = await update.message.reply_text("✅ مقدار جدید دریافت شد. برای تایید، روی دکمه تایید کلیک کنید.")
    context.user_data.setdefault("bot_messages", []).append(msg.message_id)


async def handle_go_to_wallet_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except BadRequest:
        try:
            await q.edit_message_text("در حال انتقال به کیف پول…")
        except Exception:
            pass

    context.user_data.pop("awaiting_confirm_plan", None)
    context.user_data.pop("selected_plan_id", None)
    context.user_data.pop("current_plan", None)
    context.user_data.pop("cancel_renewal_plan_id", None)
    context.user_data.pop("plan_price", None)
    context.user_data.pop("wallet_balance", None)

    await show_wallet_panel(update, context)


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_error_handler(error_handler)
    app.job_queue.run_repeating(check_expiry_dates, interval=3600, first=60)
    app.job_queue.run_repeating(check_traffic_usage, interval=3600, first=10)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(CommandHandler("wallet", show_wallet_panel))
    app.add_handler(
        CallbackQueryHandler(handle_promo_callbacks, pattern=r"^promo_")
    )
    app.add_handler(CallbackQueryHandler(handle_sync_callbacks, pattern=r"^sync_"))
    app.add_handler(CallbackQueryHandler(handle_user_message_callback, pattern=r"^user_message_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_wallet_confirm_cb, pattern="^wallet_(confirm|cancel)$"))
    app.add_handler(CallbackQueryHandler(handle_wallet_amount_preset_callback, pattern=r"^wallet_amount_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_nowpayment_callback, pattern=r"^(nowpayment_wallet|nowpayment_buy_\d+|check_nowpayment_.+)$"))
    app.add_handler(CallbackQueryHandler(handle_reward_callback, pattern=r"^reward_(approve|reject)_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_wallet_admin_cb, pattern=r"^wallet_(appr|rej)_\d+$"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_wallet_receipt_photo))
    app.add_handler(CallbackQueryHandler(handle_my_plans_nav, pattern="^my_plans_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_selection))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex("^(💰 خرید سرور|🎁 دریافت سرور تست)$"), handle_buy_server))
    app.add_handler(CallbackQueryHandler(handle_go_to_wallet_cb, pattern="^go_to_wallet$"))
    app.add_handler(CallbackQueryHandler(handle_panel_shortcuts, pattern=r"^(buy_panel_open|buy_panel_test|admin_panel_users|admin_panel_servers|admin_panel_support|admin_panel_sales)$"))
    app.add_handler(CallbackQueryHandler(handle_profile_panel, pattern=r"^(profile_user_info|profile_custom_server|profile_referral|profile_reward)$"))
    app.add_handler(
        CallbackQueryHandler(handle_user_view_category_callback, pattern=r"^user_view_category_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_back_navigation, pattern="^user_back$"))
    app.add_handler(CallbackQueryHandler(handle_user_plans_callback, pattern=r"user_plans_\d+(_\d+)?"))
    app.add_handler(CallbackQueryHandler(handle_users_pagination_callback, pattern=r"^users_page_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_reply_to_user_callback, pattern=r"^reply_to_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_show_all_support_messages_callback, pattern=r"^all_message_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_unanswered_support_messages_callback, pattern=r"^unanswered_message_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_user_cart_vis_callback, pattern=r"^user_cart_vis_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_user_transactions_callback, pattern=r"^user_transactions_\d+(_\d+)?$"))
    app.add_handler(CallbackQueryHandler(handle_user_change_balance_callback, pattern=r"^user_change_balance_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_user_delete_callback, pattern=r"^user_delete_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_user_delete_callback, pattern=r"^confirm_user_delete_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_user_discount_percentage_callback, pattern=r"^user_discount_percentage_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_selected_user_callback, pattern=r"^selected_user_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_user_in_active_callback, pattern=r"^user_in_active_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_confirm_user_in_active_callback, pattern=r"^confirm_user_(in_)?active_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_return_buy_server, pattern="return_buy_server"))
    app.add_handler(CallbackQueryHandler(handle_back_to_user_manage_menu, pattern="^back_to_user_manage_menu$"))
    app.add_handler(CallbackQueryHandler(handle_cancel_renewal_callback, pattern="cancel_renewal"))
    app.add_handler(CallbackQueryHandler(handle_search_user_by_callback, pattern=r"^search_user_(username|userid)$"))
    app.add_handler(CallbackQueryHandler(handle_search_user_callback, pattern="search_user"))
    app.add_handler(CallbackQueryHandler(handle_back_admin_panel_callback, pattern="back_admin_panel"))
    app.add_handler(CallbackQueryHandler(show_detail_purchased_plan_callback, pattern="^show_detail_purchased_plan_"))
    app.add_handler(CallbackQueryHandler(handle_qr_code_purchased_plan_callback, pattern="qr_code_purchased_plan"))
    app.add_handler(CallbackQueryHandler(handle_return_user_purchased_callback, pattern="return_user_purchased"))
    app.add_handler(CallbackQueryHandler(handle_return_my_profile_callback, pattern="return_my_profile"))
    app.add_handler(CallbackQueryHandler(handle_cancel_delete_purchased_plan, pattern="cancel_delete_purchased_plan"))
    app.add_handler(
        CallbackQueryHandler(handle_user_confirm_delete_purchased_plan, pattern="confirm_delete_purchased_plan"))
    app.add_handler(CallbackQueryHandler(handle_return_setup_server_menu_callback, pattern="return_setup_server_menu"))
    app.add_handler(CallbackQueryHandler(handle_edit_server_callback, pattern=r"^edit_server_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_renewal_plan_callback, pattern=r"^confirm_renewal_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_user_delete_purchased_plan_callback, pattern=r"^delete_purchased_plan_\d+$"))
    app.add_handler(CallbackQueryHandler(show_detail_purchased_plan_callback, pattern=r"^user_purchased_plan_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_user_renewal_purchased_callback, pattern=r"^renewal_purchased_plan_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_dis_able_purchased_plan_callback, pattern=r"^dis_able_purchased_plan_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_change_link_callback, pattern=r"^change_link_purchased_plan_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_change_name_purchased_plan_callback, pattern=r"^change_name_purchased_plan_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_user_buy_plan_callback, pattern=r"^user_buy_plan_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_buy_plan_callback, pattern=r"^confirm_buy_plan_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_view_plans_callback, pattern=r"^(admin_view_plan_|delete_plan_)\d+$"))
    app.add_handler(CallbackQueryHandler(handle_edit_plan_server_callback, pattern=r"^edit_plan_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_edit_category_callback, pattern=r"^(admin_edit|admin_delete)_category_\d+$"))
    app.add_handler(
        CallbackQueryHandler(handle_edit_field_plan_callback,
                             pattern=r"^edit_plan_(price|inbound_id|traffic_gb|duration_days)$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_edit_plan_callback,
                                         pattern=r"^confirm_edit_plan_(price|inbound_id|traffic_gb|duration_days)_[0-9]+$"))
    app.add_handler(CallbackQueryHandler(handle_edit_server_field_callback, pattern=r"^callback_edit_server_\w+_\d+$"))
    app.add_handler(CallbackQueryHandler(handle_confirm_edit_server_callback, pattern=r"^confirm_edit_\w+_\d+$"))
    app.add_handler(CallbackQueryHandler(check_join, pattern="check_join"))
    app.add_handler(CallbackQueryHandler(handle_server_callback,
                                         pattern=r"^(server|delete_server)_\d+$|^return_servers$"))
    app.add_handler(CallbackQueryHandler(handle_test_config_callback,
                                         pattern="^(edit_test_config|delete_test_config)$"))
    app.add_handler(CallbackQueryHandler(handle_server_plans_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
