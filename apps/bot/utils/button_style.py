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
    now = time.monotonic()
    cfg = _cache_value if (_cache_value is not None and now < _cache_expires_at) else _default_settings()
    if not cfg.get("enabled", True):
        return None
    style = str(cfg.get(role) or "").strip()
    if style in ("primary", "success", "danger"):
        return style
    return None


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
    now = time.monotonic()
    cfg = _cache_value if (_cache_value is not None and now < _cache_expires_at) else _default_settings()
    return bool(cfg.get("enabled", True))


def _is_header_or_noop(text: Any, callback_data: Any) -> bool:
    cb = str(callback_data or "")
    if not cb or "noop" in cb:
        return True
    t = str(text or "").strip()
    return t.startswith(("━", "─", "┄"))


def install_global_button_coloring() -> None:
    """Monkeypatch InlineKeyboardBuilder.as_markup so every callback button is
    colored by default. Idempotent; call once at bot startup. The coloring is
    still gated by the operator's `enabled` flag (via _resolve_style)."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    if getattr(InlineKeyboardBuilder, "_global_color_patched", False):
        return

    _orig_as_markup = InlineKeyboardBuilder.as_markup

    def _patched_as_markup(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        markup = _orig_as_markup(self, *args, **kwargs)
        try:
            if _coloring_enabled():
                rows = getattr(markup, "inline_keyboard", None)
                for row in (rows or []):
                    for btn in row:
                        if (
                            getattr(btn, "callback_data", None)
                            and not getattr(btn, "style", None)
                            and not _is_header_or_noop(getattr(btn, "text", ""), btn.callback_data)
                        ):
                            style = _resolve_style(_heuristic_role(btn.callback_data))
                            if style:
                                try:
                                    btn.style = style
                                except Exception:
                                    pass
        except Exception:
            logger.debug("global button coloring skipped", exc_info=True)
        return markup

    InlineKeyboardBuilder.as_markup = _patched_as_markup
    InlineKeyboardBuilder._global_color_patched = True
    logger.info("global inline-button coloring installed")
