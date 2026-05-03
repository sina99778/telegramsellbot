from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from aiogram.client.default import Default
from aiogram.methods import TelegramMethod
from sqlalchemy.exc import SQLAlchemyError

from core.config import settings
from core.database import AsyncSessionFactory
from repositories.settings import AppSettingsRepository

logger = logging.getLogger(__name__)


DEFAULT_EMOJI_KEYS: dict[str, str] = {
    "success": "✅",
    "error": "❌",
    "warning": "⚠️",
    "info": "ℹ️",
    "rocket": "🚀",
    "sparkles": "✨",
    "fire": "🔥",
    "gift": "🎁",
    "wallet": "💳",
    "money": "💰",
    "support": "💬",
    "server": "🖥",
    "config": "📋",
    "chart": "📊",
    "settings": "⚙️",
    "back": "🔙",
    "lock": "🔒",
    "ban": "🚫",
    "refresh": "🔄",
    "link": "🔗",
    "name": "📛",
    "box": "📦",
    "disk": "💾",
    "network": "📶",
    "calendar": "📅",
    "antenna": "📡",
    "trash": "🗑",
    "shuffle": "🔀",
    "red_circle": "🔴",
    "green_circle": "🟢",
    "apple": "🍎",
    "cart": "🛒",
    "profile": "👤",
    "numbers": "🔢",
    "hourglass": "⏳",
    "prev": "◀️",
    "next": "▶️",
    "down": "👇",
    "wave": "👋",
    "lightning": "⚡",
    "diamond": "🔸",
    "star": "✨",
    "pin": "📍",
}

HTML_CODE_RE = re.compile(r"(<(?:code|pre)\b[^>]*>.*?</(?:code|pre)>|<tg-emoji\b[^>]*>.*?</tg-emoji>)", re.I | re.S)
VALID_CUSTOM_EMOJI_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
_CACHE_TTL_SECONDS = 30


@dataclass(slots=True)
class PremiumEmojiRuntimeSettings:
    enabled: bool
    emoji_map: dict[str, str]


_cache_value: PremiumEmojiRuntimeSettings | None = None
_cache_expires_at = 0.0


def parse_emoji_map_text(text: str) -> dict[str, str]:
    stripped = text.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        data = json.loads(stripped)
        if not isinstance(data, dict):
            raise ValueError("مپ اموجی در حالت JSON باید یک آبجکت باشد.")
        return _sanitize_emoji_map(data)

    parsed: dict[str, str] = {}
    for line in stripped.splitlines():
        clean = line.strip()
        if not clean:
            continue
        separator = "=" if "=" in clean else ":"
        if separator not in clean:
            raise ValueError("هر خط مپ اموجی باید به شکل کلید=emoji_id باشد.")
        key, value = clean.split(separator, 1)
        parsed[key.strip()] = value.strip()
    return _sanitize_emoji_map(parsed)


async def get_runtime_premium_emoji_settings() -> PremiumEmojiRuntimeSettings:
    global _cache_expires_at, _cache_value

    now = time.monotonic()
    if _cache_value is not None and now < _cache_expires_at:
        return _cache_value

    fallback = PremiumEmojiRuntimeSettings(
        enabled=settings.premium_emoji_enabled,
        emoji_map=_sanitize_emoji_map(settings.premium_emoji_map),
    )
    try:
        async with AsyncSessionFactory() as session:
            db_settings = await AppSettingsRepository(session).get_premium_emoji_settings()
            value = PremiumEmojiRuntimeSettings(
                enabled=db_settings.enabled or fallback.enabled,
                emoji_map={**fallback.emoji_map, **_sanitize_emoji_map(db_settings.emoji_map)},
            )
    except (SQLAlchemyError, OSError, RuntimeError) as exc:
        logger.warning("Using env premium emoji settings because DB lookup failed: %s", exc)
        value = fallback

    _cache_value = value
    _cache_expires_at = now + _CACHE_TTL_SECONDS
    return value


def clear_premium_emoji_cache() -> None:
    global _cache_expires_at, _cache_value
    _cache_value = None
    _cache_expires_at = 0.0


async def apply_premium_emoji_to_method(method: TelegramMethod[Any], *, default_parse_mode: str | None) -> None:
    runtime = await get_runtime_premium_emoji_settings()
    if not runtime.enabled or not runtime.emoji_map:
        return

    if hasattr(method, "text") and isinstance(getattr(method, "text"), str):
        if _method_uses_html(method, default_parse_mode, entities_attr="entities"):
            method.text = render_premium_emoji_html(method.text, runtime.emoji_map)

    if hasattr(method, "caption") and isinstance(getattr(method, "caption"), str):
        if _method_uses_html(method, default_parse_mode, entities_attr="caption_entities"):
            method.caption = render_premium_emoji_html(method.caption, runtime.emoji_map)


def render_premium_emoji_html(text: str, emoji_map: dict[str, str]) -> str:
    sanitized_map = _sanitize_emoji_map(emoji_map)
    if not text or not sanitized_map:
        return text

    replacements = _build_replacements(sanitized_map)
    if not replacements:
        return text

    parts = HTML_CODE_RE.split(text)
    for index, part in enumerate(parts):
        if index % 2 == 1:
            continue
        for fallback, custom_emoji_id in replacements:
            tag = f'<tg-emoji emoji-id="{html.escape(custom_emoji_id, quote=True)}">{fallback}</tg-emoji>'
            part = part.replace(fallback, tag)
        parts[index] = part
    return "".join(parts)


def _build_replacements(emoji_map: dict[str, str]) -> list[tuple[str, str]]:
    replacements: list[tuple[str, str]] = []
    for key, custom_emoji_id in emoji_map.items():
        fallback = DEFAULT_EMOJI_KEYS.get(key, key)
        if fallback:
            replacements.append((fallback, custom_emoji_id))
    replacements.sort(key=lambda item: len(item[0]), reverse=True)
    return replacements


def _sanitize_emoji_map(raw_map: dict[str, Any]) -> dict[str, str]:
    sanitized: dict[str, str] = {}
    if not isinstance(raw_map, dict):
        return sanitized
    for key, value in raw_map.items():
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if clean_key and VALID_CUSTOM_EMOJI_ID_RE.match(clean_value):
            sanitized[clean_key] = clean_value
    return sanitized


def _method_uses_html(method: TelegramMethod[Any], default_parse_mode: str | None, *, entities_attr: str) -> bool:
    if getattr(method, entities_attr, None):
        return False
    parse_mode = getattr(method, "parse_mode", None)
    if isinstance(parse_mode, Default):
        parse_mode = default_parse_mode
    parse_mode_text = str(parse_mode or "").lower()
    return "html" in parse_mode_text
