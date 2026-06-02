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


def _resolve_style(role: str) -> str | None:
    """Translate a semantic role to a Telegram style string, or None.

    Reads from the (sync) cache. If the cache is empty or stale, falls
    back to defaults — never blocks. The first hot call after a deploy
    just uses defaults until prime_button_style_cache() runs.
    """
    if role not in _VALID_ROLES:
        return None
    # Use the primed cache whenever present (no TTL-revert-to-defaults). The
    # cache is refreshed on settings changes via Redis pub/sub (core.cache_sync),
    # so it never goes stale — and the operator's custom colors don't silently
    # revert after 30s the way the old TTL made them.
    cfg = _cache_value if _cache_value is not None else _default_settings()
    if not cfg.get("enabled", True):
        return None
    style = str(cfg.get(role) or "").strip()
    if style in ("primary", "success", "danger"):
        return style
    return None


def make_keyboard_button(text: str, *, role: str | None = None, **kwargs: Any):
    """Build a REPLY-keyboard KeyboardButton, colored by `role`.

    Bot API 9.4 added the `style` field to KeyboardButton too (not just inline),
    so the bottom menu can be colored as well. Falls back to an uncolored button
    on older aiogram or when the operator has coloring disabled.
    """
    from aiogram.types import KeyboardButton

    style = _resolve_style(role) if role else None
    btn: KeyboardButton
    if style:
        try:
            btn = KeyboardButton(text=text, style=style, **kwargs)
        except TypeError:
            btn = KeyboardButton(text=text, **kwargs)
    else:
        btn = KeyboardButton(text=text, **kwargs)
    # Premium icon: moves the leading emoji into icon_custom_emoji_id and strips
    # it from the text. Safe for REPLY buttons now that the menu handlers route
    # via MenuText (emoji-insensitive), so the stripped text still matches.
    _apply_premium_icon(btn)
    return btn


def styled_button(
    builder: InlineKeyboardBuilder,
    text: str,
    *,
    callback_data: str | None = None,
    role: str | None = None,
    **kwargs: Any,
) -> None:
    """Add a button to `builder`, optionally with a colored `style`.

    Falls back gracefully if the installed aiogram is older than 3.27
    (no `style` kwarg) — the button still gets added, just uncolored.
    """
    if role:
        style = _resolve_style(role)
        if style:
            try:
                builder.button(text=text, callback_data=callback_data, style=style, **kwargs)
                return
            except TypeError:
                # aiogram doesn't accept `style` yet → drop it silently.
                pass
    builder.button(text=text, callback_data=callback_data, **kwargs)


# A leading emoji "cluster": the emoji + any variation selectors / ZWJ / skin
# tones / keycap + surrounding spaces. Persian letters and ZWNJ (U+200C) are
# deliberately NOT in the class, so Persian labels are never damaged.
_LEADING_EMOJI_RUN_RE = re.compile(
    "^[\\s"
    "\U0001F000-\U0001FAFF"   # pictographs / emoji
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U00002190-\U000021FF"   # arrows
    "\U000025A0-\U000025FF"   # geometric shapes (◀️ ▶️ …)
    "\U00002B00-\U00002BFF"   # misc symbols & arrows (⭐ …)
    "\U0001F1E6-\U0001F1FF"   # regional indicators
    "\U00002139\U0000231A\U0000231B\U000023E9-\U000023FA"
    "️‍⃣\U0001F3FB-\U0001F3FF"  # VS16, ZWJ, keycap, skin tones
    "]+"
)


def strip_leading_emoji(text: str) -> str:
    """Drop a leading emoji cluster (and surrounding spaces) from a label.

    Used so reply-menu routing matches a button whether or not premium-emoji
    icons stripped its emoji into icon_custom_emoji_id.
    """
    if not text:
        return text
    return _LEADING_EMOJI_RUN_RE.sub("", text, count=1).strip()


# ── Global default coloring ──────────────────────────────────────────────
# Most keyboards are built with a plain `builder.button(...)` (no role), so
# they never got a color. To color the WHOLE bot without touching hundreds of
# call sites, we patch InlineKeyboardBuilder so that — at markup-build time —
# every callback button that DIDN'T ask for its own color gets the operator's
# default ("navigation") color. Buttons that set a style explicitly (via
# styled_button, e.g. the green/red items on the main menu) keep it, and
# section-header / noop buttons stay uncolored.

_DEFAULT_COLOR_ROLE = "navigation"

# High-confidence callback_data substrings → role, so the whole bot gets the
# same tasteful blue/green/red mix as the hand-tagged main menu instead of a
# monotone blue. Anything that doesn't match stays "navigation" (blue).
_RED_HINTS = (
    "delete", "remove", "revoke", "reject", "wipe", "cancel", "reset",
    "clear", "disable", "destroy", "toggle_ban", "ban_user", "unban",
    "txcf:no", "mp:no", ":no:",
)
_GREEN_HINTS = (
    "confirm", "approve", ":pay", "buy", "purchase", "renew", "topup",
    "gift", "create", "enable", "activate", ":ok", ":yes", ":final", "save", "submit",
)


def _heuristic_role(callback_data: Any) -> str:
    cb = str(callback_data or "").lower()
    for hint in _RED_HINTS:
        if hint in cb:
            return "destructive"
    for hint in _GREEN_HINTS:
        if hint in cb:
            return "confirm"
    return _DEFAULT_COLOR_ROLE


def _coloring_enabled() -> bool:
    cfg = _cache_value if _cache_value is not None else _default_settings()
    return bool(cfg.get("enabled", True))


def _is_header_or_noop(text: Any, callback_data: Any) -> bool:
    cb = str(callback_data or "")
    if not cb or "noop" in cb:
        return True
    t = str(text or "").strip()
    return t.startswith(("━", "─", "┄"))


# ── Premium-emoji icons on buttons (Bot API 9.4 icon_custom_emoji_id) ─────
# A button can carry a custom (premium / animated) emoji icon. We bridge the
# operator's existing premium-emoji map (standard emoji → custom_emoji_id) to
# the button icon: when a label starts with a mapped emoji, we move that emoji
# into icon_custom_emoji_id and drop it from the visible text. Primed once at
# startup and refreshed when the operator edits premium-emoji settings — NOT
# TTL-based, so a configured icon never silently disappears mid-session.

_premium_icons_list: list[tuple[str, str]] | None = None  # [(emoji, custom_id)] longest-first
_premium_icons_enabled = False


def clear_premium_icon_cache() -> None:
    global _premium_icons_list
    _premium_icons_list = None


async def prime_premium_icon_cache() -> None:
    """Load the premium-emoji map into the sync cache used by the button patch."""
    global _premium_icons_list, _premium_icons_enabled
    items: list[tuple[str, str]] = []
    enabled = False
    try:
        async with AsyncSessionFactory() as session:
            s = await AppSettingsRepository(session).get_premium_emoji_settings()
            enabled = bool(s.enabled)
            from services.telegram.premium_emoji import _build_replacements
            items = [(fb, cid) for fb, cid in _build_replacements(s.emoji_map or {}) if fb and cid]
    except Exception as exc:
        logger.warning("premium icon cache prime failed: %s", exc)
    _premium_icons_list = items
    _premium_icons_enabled = enabled


def _apply_premium_icon(btn: Any) -> None:
    """If the button label starts with a premium-mapped emoji, set the button's
    custom-emoji icon and strip that emoji from the text. No-op unless premium
    emoji is enabled and configured."""
    if not _premium_icons_enabled or not _premium_icons_list:
        return
    if getattr(btn, "icon_custom_emoji_id", None):
        return
    text = getattr(btn, "text", None)
    if not text:
        return
    stripped = text.lstrip()
    for emoji, custom_id in _premium_icons_list:
        if stripped.startswith(emoji):
            rest = stripped[len(emoji):].lstrip()
            if not rest:
                return  # emoji-only label → leave the text emoji, no icon
            try:
                btn.icon_custom_emoji_id = custom_id
                btn.text = rest
            except Exception:
                pass
            return


def install_global_button_coloring() -> None:
    """Monkeypatch InlineKeyboardBuilder.as_markup so every inline button gets
    (a) a default color and (b) a premium-emoji icon when configured. Idempotent;
    call once at bot startup. Both effects are gated by their own operator flags."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    if getattr(InlineKeyboardBuilder, "_global_color_patched", False):
        return

    _orig_as_markup = InlineKeyboardBuilder.as_markup

    def _patched_as_markup(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        markup = _orig_as_markup(self, *args, **kwargs)
        try:
            color_on = _coloring_enabled()
            icon_on = bool(_premium_icons_enabled and _premium_icons_list)
            if color_on or icon_on:
                for row in (getattr(markup, "inline_keyboard", None) or []):
                    for btn in row:
                        if _is_header_or_noop(getattr(btn, "text", ""), getattr(btn, "callback_data", None)):
                            continue
                        if (
                            color_on
                            and getattr(btn, "callback_data", None)
                            and not getattr(btn, "style", None)
                        ):
                            style = _resolve_style(_heuristic_role(btn.callback_data))
                            if style:
                                try:
                                    btn.style = style
                                except Exception:
                                    pass
                        if icon_on:
                            _apply_premium_icon(btn)
        except Exception:
            logger.debug("global button decoration skipped", exc_info=True)
        return markup

    InlineKeyboardBuilder.as_markup = _patched_as_markup
    InlineKeyboardBuilder._global_color_patched = True
    logger.info("global inline-button coloring installed")
