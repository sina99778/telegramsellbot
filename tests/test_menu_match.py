"""Tests for emoji-insensitive menu routing (strip_leading_emoji + MenuText).

These guard the fix that lets the bottom-menu buttons keep working when premium
emoji icons strip the leading emoji from a reply button's text.
"""
from __future__ import annotations

from types import SimpleNamespace

from apps.bot.utils.button_style import strip_leading_emoji
from apps.bot.utils.menu_match import MenuText


def test_strip_leading_emoji_persian():
    assert strip_leading_emoji("🛒 خرید سرویس") == "خرید سرویس"


def test_strip_preserves_zwnj():
    # The ZWNJ (U+200C) inside Persian words must NOT be removed.
    assert strip_leading_emoji("📋 سرویس‌های من") == "سرویس‌های من"


def test_strip_no_emoji_unchanged():
    assert strip_leading_emoji("خرید سرویس") == "خرید سرویس"


def test_strip_variation_selector():
    # ⚙️ = U+2699 + U+FE0F — both must go.
    assert strip_leading_emoji("⚙️ پنل مدیریت") == "پنل مدیریت"


def test_strip_empty():
    assert strip_leading_emoji("") == ""


async def test_menutext_matches_stripped_and_full():
    f = MenuText("🛒 خرید سرویس")
    # premium-on sends the stripped text; premium-off sends the full text.
    assert await f(SimpleNamespace(text="خرید سرویس")) is True
    assert await f(SimpleNamespace(text="🛒 خرید سرویس")) is True


async def test_menutext_rejects_others():
    f = MenuText("🛒 خرید سرویس")
    assert await f(SimpleNamespace(text="سلام")) is False
    assert await f(SimpleNamespace(text=None)) is False


async def test_menutext_multiple_labels():
    f = MenuText("⚙️ پنل مدیریت", "پنل مدیریت ⚙️")
    assert await f(SimpleNamespace(text="پنل مدیریت")) is True
