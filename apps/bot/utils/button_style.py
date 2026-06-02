"""
Helper for Bot API 9.4's inline keyboard button colors.

Bot API 9.4 (Feb 2026) added a `style` field on `InlineKeyboardButton`
and `KeyboardButton`. The accepted values are:

    "primary"  → blue
    "success"  → green
    "danger"   → red
    ""/None    → Telegram's default look

We expose those colors via SEMANTIC ROLES, not raw Telegram styles, so
the keyboard code can stay intent-driven and the operator can remap
each role to whichever color they want via the bot-admin / dashboard
settings panel. Roles:

    "confirm"     — positive actions (buy, save, send)
    "destructive" — irreversible (delete, ban, wipe)
    "navigation"  — back / forward / main-menu
    "info"        — neutral but emphasised (stats, view)

Usage:

    from apps.bot.utils.button_style import styled_button

    builder = InlineKeyboardBuilder()
    styled_button(builder, "حذف", callback_data="...", role="destructive")
    styled_button(builder, "بازگشت", callback_data="...", role="navigation")

`styled_button` reads from a 30-second cache so it never blocks the
keyboard build on a DB round-trip. The cache is refreshed by
`clear_button_style_cache()` after the operator changes settings.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.exc import SQLAlchemyError

from core.database import AsyncSessionFactory
from repositories.settings import AppSettingsRepository, ButtonStyleSettings


logger = logging.getLogger(__name__)


_VALID_ROLES = {"confirm", "destructive", "navigation", "info"}
_CACHE_TTL_SECONDS = 30

# Each color token → (colored-circle emoji prefix, native Telegram style|None).
# The emoji prefix is what makes the color visible on EVERY Telegram client
# (the native `style` only renders on the newest apps). violet/amber/orange
# have no native equivalent, so they're emoji-only.
COLOR_PALETTE: dict[str, tuple[str, str | None]] = {
    "success": ("🟢", "success"),
    "danger": ("🔴", "danger"),
    "primary": ("🔵", "primary"),
    "violet": ("🟣", "primary"),
    "amber": ("🟡", None),
    "orange": ("🟠", None),
    "": ("", None),
}

# Detect a leading emoji so we never double up (e.g. "🔙 بازگشت" stays as-is).
# The ranges cover pictographic emoji, symbols, arrows and dingbats but NOT
# Arabic/Persian letters (U+0600–06FF), so Persian labels are prefixed.
_LEADING_EMOJI_RE = re.compile(
    "^["
    "\U0001F000-\U0001FAFF"   # pictographs / emoji
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002190-\U000021FF"   # arrows
    "\U000025A0-\U000025FF"   # geometric shapes (◀️ ▶️ ● ■ …)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows (⭐ …)
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U00002139\U0000231A-\U0000231B\U000023E9-\U000023FA"
    "]"
)


def _has_leading_emoji(text: str) -> bool:
    return bool(_LEADING_EMOJI_RE.match((text or "").lstrip()))


def color_emoji(token: str | None) -> str:
    """Public helper: the colored-circle emoji for a color token (for the
    bot/web settings preview)."""
    return COLOR_PALETTE.get((token or "").strip(), ("", None))[0]

_cache_value: dict[str, Any] | None = None
_cache_expires_at = 0.0


def clear_button_style_cache() -> None:
    """Call after an operator changes the role→color mapping so the
    next keyboard render picks up the new values."""
    global _cache_value, _cache_expires_at
    _cache_value = None
    _cache_expires_at = 0.0


def _default_settings() -> dict[str, Any]:
    return {
        "enabled": True,
        "confirm": "success",
        "destructive": "danger",
        "navigation": "primary",
        "info": "primary",
    }


async def prime_button_style_cache() -> dict[str, Any]:
    """Refresh the cache from the DB. Returns the cached dict."""
    global _cache_value, _cache_expires_at
    settings = _default_settings()
    try:
        async with AsyncSessionFactory() as session:
            db_settings = await AppSettingsRepository(session).get_button_style_settings()
            settings = asdict(db_settings)
    except (SQLAlchemyError, OSError, RuntimeError) as exc:
        logger.warning("button style fetch failed, using defaults: %s", exc)
    _cache_value = settings
    _cache_expires_at = time.monotonic() + _CACHE_TTL_SECONDS
    return settings


def _resolve_color_token(role: str) -> str:
    """Translate a semantic role to the operator-chosen color token.

    Reads from the (sync) cache. If the cache is empty or stale, falls
    back to defaults — never blocks. Returns "" when coloring is disabled
    or the role is unknown.
    """
    if role not in _VALID_ROLES:
        return ""
    now = time.monotonic()
    cfg = _cache_value if (_cache_value is not None and now < _cache_expires_at) else _default_settings()
    if not cfg.get("enabled", True):
        return ""
    token = str(cfg.get(role) or "").strip()
    return token if token in COLOR_PALETTE else ""


def _resolve_style(role: str) -> str | None:
    """Back-compat: the native Telegram style for a role (or None)."""
    return COLOR_PALETTE.get(_resolve_color_token(role), ("", None))[1]


def styled_button(
    builder: InlineKeyboardBuilder,
    text: str,
    *,
    callback_data: str | None = None,
    role: str | None = None,
    **kwargs: Any,
) -> None:
    """Add a button to `builder`, colored by its semantic `role`.

    Coloring is applied two ways so it's visible everywhere:
      1. A colored-circle emoji prefix (🟢/🔵/🔴/🟣/🟡/🟠) — unless the label
         already starts with an emoji. Shows on every Telegram client.
      2. The native Bot API 9.4 `style` field — shows on the newest apps.

    Falls back gracefully on older aiogram (no `style` kwarg): the button is
    still added, just without the native style (the emoji prefix remains).
    """
    label = text
    style: str | None = None
    if role:
        token = _resolve_color_token(role)
        if token:
            emoji, style = COLOR_PALETTE.get(token, ("", None))
            if emoji and not _has_leading_emoji(text):
                label = f"{emoji} {text}"

    if style:
        try:
            builder.button(text=label, callback_data=callback_data, style=style, **kwargs)
            return
        except TypeError:
            # aiogram doesn't accept `style` yet → drop it but keep the emoji.
            pass
    builder.button(text=label, callback_data=callback_data, **kwargs)
