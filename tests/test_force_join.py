"""Tests for force-join channel normalisation (apps/bot/middlewares/force_join).

The bug: users joined the channel but the bot still blocked them. Root cause
was the membership check failing (bot not admin / channel stored without "@")
and the middleware failing CLOSED. The fix fails OPEN on a verify-error and
normalises the channel id — these tests cover the normalisation.
"""
from __future__ import annotations

from apps.bot.middlewares.force_join import _normalize_channel


def test_bare_username_gets_at():
    assert _normalize_channel("mychannel") == "@mychannel"


def test_at_username_unchanged():
    assert _normalize_channel("@mychannel") == "@mychannel"


def test_numeric_id_unchanged():
    assert _normalize_channel("-1001234567890") == "-1001234567890"


def test_whitespace_trimmed():
    assert _normalize_channel("  mychannel  ") == "@mychannel"


def test_tme_link_extracted():
    assert _normalize_channel("https://t.me/mychannel") == "@mychannel"
    assert _normalize_channel("t.me/mychannel/") == "@mychannel"


def test_private_invite_left_as_is():
    # A +invite link can't become an @username; leave it (the check fails open).
    assert _normalize_channel("https://t.me/+abc123") == "https://t.me/+abc123"


def test_empty():
    assert _normalize_channel("") == ""
    assert _normalize_channel(None) == ""  # type: ignore[arg-type]
