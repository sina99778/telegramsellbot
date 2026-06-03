"""Tests for PasarGuard runtime helpers + subscription-URL resolution."""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from schemas.internal.pasarguard import PGUserResponse, make_absolute_sub_url
from services.pasarguard.runtime import absolute_sub_url, build_pasarguard_client_config


def test_absolute_sub_url_relative_is_prefixed_with_origin_only():
    # base URL carries a path (/dashboard) — the sub URL must hang off the
    # ORIGIN, not the path.
    server = NS(base_url="https://panel.example.com:8443/dashboard")
    assert absolute_sub_url(server, "/sub/tok/") == "https://panel.example.com:8443/sub/tok/"


def test_absolute_sub_url_absolute_passes_through():
    server = NS(base_url="https://panel.example.com")
    assert absolute_sub_url(server, "https://s.example.com/sub/y") == "https://s.example.com/sub/y"


def test_absolute_sub_url_empty():
    server = NS(base_url="https://panel.example.com")
    assert absolute_sub_url(server, "") == ""
    assert absolute_sub_url(server, None) == ""


def test_make_absolute_sub_url_defaults_scheme_when_missing():
    assert make_absolute_sub_url("1.2.3.4:8000", "/sub/z") == "http://1.2.3.4:8000/sub/z"


def test_make_absolute_sub_url_adds_leading_slash():
    assert make_absolute_sub_url("http://h:8000", "sub/z") == "http://h:8000/sub/z"


def test_response_helper_matches_module_function():
    r = PGUserResponse(username="u", subscription_url="/sub/abc/")
    assert r.absolute_subscription_url("http://h:9000") == "http://h:9000/sub/abc/"


def test_build_config_requires_credentials():
    with pytest.raises(ValueError):
        build_pasarguard_client_config(NS(base_url="http://x", credentials=None))
