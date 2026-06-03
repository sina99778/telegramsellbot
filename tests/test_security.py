"""Tests for the secret-key fingerprint used by the DR / backup safety net."""
from __future__ import annotations

from core.security import secret_key_fingerprint


def test_fingerprint_is_deterministic():
    assert secret_key_fingerprint() == secret_key_fingerprint()


def test_fingerprint_is_short_hex_and_one_way():
    fp = secret_key_fingerprint()
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


def test_fingerprint_changes_with_key(monkeypatch):
    from core.config import settings
    from pydantic import SecretStr

    fp_a = secret_key_fingerprint()
    monkeypatch.setattr(settings, "app_secret_key", SecretStr("a-totally-different-key"))
    fp_b = secret_key_fingerprint()
    assert fp_a != fp_b  # a different key → a different fingerprint
