"""
Regression tests for finding #67: build_sub_link hard-coded '/sub/' while
3x-ui's subscription path (subPath) is operator-configurable.

The fix adds core.config.settings.xui_sub_path (default "sub") and makes
services.xui.runtime.build_sub_link honour it, stripping slashes defensively.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.config import Settings, settings
from services.xui.runtime import build_sub_link


def _make_server(**overrides):
    base = dict(
        base_url="https://panel.example.com:54321/path",
        sub_domain=None,
        subscription_port=2096,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_default_sub_path_is_sub():
    """Settings default keeps the stock 3x-ui '/sub/' behaviour."""
    assert Settings.model_fields["xui_sub_path"].default == "sub"


def test_build_sub_link_default_unchanged():
    """With the default setting the link is byte-identical to the old one."""
    server = _make_server()
    assert build_sub_link(server, "abc123") == "http://panel.example.com:2096/sub/abc123"


def test_build_sub_link_uses_custom_sub_path(monkeypatch):
    """A custom subPath (operator-randomized) is honoured."""
    monkeypatch.setattr(settings, "xui_sub_path", "mysecretpath")
    server = _make_server()
    assert build_sub_link(server, "abc123") == (
        "http://panel.example.com:2096/mysecretpath/abc123"
    )


@pytest.mark.parametrize("raw", ["/custom/", "custom/", "/custom", " /custom/ "])
def test_build_sub_link_strips_slashes_and_whitespace(monkeypatch, raw):
    """Leading/trailing slashes and whitespace in the setting are stripped."""
    monkeypatch.setattr(settings, "xui_sub_path", raw)
    server = _make_server()
    assert build_sub_link(server, "abc123") == (
        "http://panel.example.com:2096/custom/abc123"
    )


@pytest.mark.parametrize("raw", ["", "   ", "/", "//"])
def test_build_sub_link_blank_setting_falls_back_to_sub(monkeypatch, raw):
    """A blank/slash-only setting falls back to the 3x-ui default 'sub'."""
    monkeypatch.setattr(settings, "xui_sub_path", raw)
    server = _make_server()
    assert build_sub_link(server, "abc123") == "http://panel.example.com:2096/sub/abc123"


def test_build_sub_link_custom_path_with_sub_domain(monkeypatch):
    """Custom subPath composes with an explicit sub_domain scheme/host."""
    monkeypatch.setattr(settings, "xui_sub_path", "hidden")
    server = _make_server(sub_domain="https://sub.example.com")
    assert build_sub_link(server, "abc123") == (
        "https://sub.example.com:2096/hidden/abc123"
    )
