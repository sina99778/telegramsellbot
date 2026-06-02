"""Tests for the bot button-coloring logic (apps/bot/utils/button_style.py)."""
from __future__ import annotations

import time

from apps.bot.utils import button_style as bs


def _prime(*, info="primary", enabled=True):
    bs._cache_value = {
        "enabled": enabled,
        "confirm": "success",
        "destructive": "danger",
        "navigation": "primary",
        "info": info,
    }
    bs._cache_expires_at = time.monotonic() + 30


def test_heuristic_role_destructive():
    assert bs._heuristic_role("config:delete:1") == "destructive"
    assert bs._heuristic_role("admin_user:toggle_ban") == "destructive"
    assert bs._heuristic_role("txcf:no") == "destructive"


def test_heuristic_role_confirm():
    assert bs._heuristic_role("wallet:topup:pay:tetrapay") == "confirm"
    assert bs._heuristic_role("buyplan:x") == "confirm"
    assert bs._heuristic_role("mp:ok:final:1") == "confirm"


def test_heuristic_role_navigation_no_false_positive():
    # ":no" must not leak into "notifications"; "node" etc. stay blue.
    assert bs._heuristic_role("admin:notifications") == "navigation"
    assert bs._heuristic_role("admin:servers") == "navigation"
    assert bs._heuristic_role("admin:main") == "navigation"


def test_resolve_style_no_ttl_revert():
    # An operator's custom mapping must survive past the old 30s TTL.
    _prime(info="success")
    bs._cache_expires_at = time.monotonic() - 999  # pretend it expired
    assert bs._resolve_style("info") == "success"  # not the default 'primary'


def test_coloring_disabled_respected():
    _prime(enabled=False)
    assert bs._coloring_enabled() is False
    assert bs._resolve_style("confirm") is None


def test_unknown_role_is_none():
    _prime()
    assert bs._resolve_style("bogus") is None
