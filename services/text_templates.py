"""
Operator-editable bot copy.

Every text in the bot/miniapp that the operator might want to tweak
(welcome message, "service activated" notice, support footer, etc.)
lives here as a `(key, default, label, group)` row. The bot reads each
template via `resolve(key)` which consults a 30 s in-process cache, so
the operator's edits propagate within half a minute of saving without
any restart.

When a template isn't overridden, `resolve(key)` returns the code-side
default — so removing all overrides reverts the bot to factory copy.

The dashboard exposes this catalogue at `/api/dashboard/text_templates`
and renders one editable textarea per row. Each row's `group` controls
which tab it lands in.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from core.database import AsyncSessionFactory
from repositories.settings import AppSettingsRepository


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TextTemplate:
    key: str
    default: str
    label: str  # human-friendly label, Persian
    group: str  # "welcome" | "purchase" | "renewal" | "wallet" | "support" | "errors"
    multiline: bool = False
    notes: str = ""


# Catalogue. Add new keys here; the dashboard auto-renders them.
CATALOGUE: tuple[TextTemplate, ...] = (
    # ── Welcome / onboarding ──────────────────────────────────────
    TextTemplate(
        key="welcome_message",
        default="سلام {first_name} 👋\nبه ربات ما خوش اومدی.",
        label="پیام خوش‌آمدگویی",
        group="welcome",
        multiline=True,
        notes="پیام اولی که کاربر بعد از /start می‌بینه. {first_name} اسم کوچک تلگرامش رو نشون می‌ده.",
    ),
    TextTemplate(
        key="welcome_returning_user",
        default="خوش برگشتی {first_name} 🌟",
        label="پیام بازگشت کاربر قدیمی",
        group="welcome",
        notes="کاربری که قبلاً ثبت‌نام کرده و دوباره /start می‌زنه.",
    ),

    # ── Purchase flow ─────────────────────────────────────────────
    TextTemplate(
        key="purchase_select_plan",
        default="📦 لطفاً یکی از پلن‌های زیر را انتخاب کن:",
        label="عنوان لیست پلن‌ها",
        group="purchase",
    ),
    TextTemplate(
        key="purchase_success_footer",
        default="موفق باشی! 🌟",
        label="ضمیمه‌ی پایان موفقیت‌آمیز خرید",
        group="purchase",
    ),

    # ── Renewal ───────────────────────────────────────────────────
    TextTemplate(
        key="renewal_success",
        default="✅ سرویس شما با موفقیت تمدید شد.",
        label="پیام موفقیت تمدید",
        group="renewal",
    ),
    TextTemplate(
        key="renewal_options_header",
        default="🔁 تمدید سرویس",
        label="عنوان منوی تمدید",
        group="renewal",
    ),

    # ── Wallet / topup ────────────────────────────────────────────
    TextTemplate(
        key="topup_methods_intro",
        default="یکی از روش‌های پرداخت زیر را انتخاب کن:",
        label="عنوان لیست روش‌های پرداخت کیف پول",
        group="wallet",
    ),
    TextTemplate(
        key="wallet_low_balance",
        default="موجودی کیف پول کافی نیست.",
        label="هشدار موجودی ناکافی",
        group="wallet",
    ),

    # ── Support ───────────────────────────────────────────────────
    TextTemplate(
        key="support_intro",
        default="در صورت بروز مشکل، با پشتیبانی تماس بگیر.",
        label="پیام بخش پشتیبانی",
        group="support",
        multiline=True,
    ),
    TextTemplate(
        key="support_handle_footer",
        default="📞 پشتیبانی: @SupportUser",
        label="ضمیمه‌ی شماره/آی‌دی پشتیبانی",
        group="support",
    ),

    # ── Generic errors ────────────────────────────────────────────
    TextTemplate(
        key="error_generic",
        default="❌ خطایی رخ داد. لطفاً دوباره تلاش کن.",
        label="پیام خطای عمومی",
        group="errors",
    ),
    TextTemplate(
        key="error_payment_unavailable",
        default="❌ درگاه پرداخت در دسترس نیست. کمی بعد دوباره تلاش کن.",
        label="پیام درگاه پرداخت غیرفعال",
        group="errors",
    ),
)


_CATALOGUE_BY_KEY: dict[str, TextTemplate] = {t.key: t for t in CATALOGUE}


# ── Cached lookup ────────────────────────────────────────────────────

_CACHE_TTL = 30.0
_cache_value: dict[str, str] | None = None
_cache_expires_at = 0.0


def clear_text_template_cache() -> None:
    global _cache_value, _cache_expires_at
    _cache_value = None
    _cache_expires_at = 0.0


async def prime_text_template_cache() -> dict[str, str]:
    global _cache_value, _cache_expires_at
    overrides: dict[str, str] = {}
    try:
        async with AsyncSessionFactory() as session:
            overrides = await AppSettingsRepository(session).get_all_text_templates()
    except (SQLAlchemyError, OSError, RuntimeError) as exc:
        logger.warning("text-template cache prime failed, using defaults: %s", exc)
    _cache_value = overrides
    _cache_expires_at = time.monotonic() + _CACHE_TTL
    return overrides


def resolve(key: str, **format_kwargs) -> str:
    """Return the (possibly-formatted) operator-overridden text, or the
    code default if not overridden.

    Format kwargs are applied via `str.format_map` with a missing-key
    fallback so a template that references {first_name} but is called
    without one doesn't crash the bot — the placeholder just renders
    as the empty string.
    """
    t = _CATALOGUE_BY_KEY.get(key)
    code_default = t.default if t else key

    now = time.monotonic()
    cache = _cache_value if (_cache_value is not None and now < _cache_expires_at) else None
    template = (cache or {}).get(key) if cache is not None else None
    if template is None:
        template = code_default

    if not format_kwargs:
        return template

    class _Safe(dict):
        def __missing__(self, _key):
            return ""
    try:
        return template.format_map(_Safe(format_kwargs))
    except Exception as exc:
        logger.warning("text-template %s render failed (%s) — falling back to default", key, exc)
        try:
            return code_default.format_map(_Safe(format_kwargs))
        except Exception:
            return code_default


def catalogue_dict() -> list[dict]:
    """Used by the dashboard endpoint to render the editor."""
    return [
        {
            "key": t.key,
            "default": t.default,
            "label": t.label,
            "group": t.group,
            "multiline": t.multiline,
            "notes": t.notes,
        }
        for t in CATALOGUE
    ]
