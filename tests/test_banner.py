"""Banner rendering tests for services.banner.create_traffic_banner.

The banner returns PNG bytes, so instead of inspecting pixels we capture the
strings handed to ImageDraw.text(). That is where the reported bug lived: an
unlimited config rendered "0.00 GB / 0.00 GB" and a red "100.0% USED" ring
because ``total_gb = 0`` fell into the "no quota" branch.
"""

import io

import pytest

from services import banner as banner_mod
from services.banner import create_traffic_banner

VLESS = "vless://uuid@example.com:443?type=ws&security=tls#test"


@pytest.fixture
def drawn(monkeypatch):
    """Collect every string drawn onto the canvas."""
    texts: list[str] = []
    original = banner_mod.ImageDraw.Draw

    def _spy_draw(image, *args, **kwargs):
        draw = original(image, *args, **kwargs)
        real_text = draw.text

        def text(xy, content, *a, **kw):
            texts.append(str(content))
            return real_text(xy, content, *a, **kw)

        draw.text = text
        return draw

    monkeypatch.setattr(banner_mod.ImageDraw, "Draw", _spy_draw)
    return texts


def _render(**overrides):
    params = dict(
        config_name="test",
        user_id=5,
        status="active",
        used_gb=0.0,
        total_gb=10.0,
        days_left=30,
        is_active=True,
        bot_username="somebot",
        vless_uri=VLESS,
    )
    params.update(overrides)
    return create_traffic_banner(**params)


def _blob(texts: list[str]) -> str:
    return " | ".join(texts)


def test_unlimited_traffic_is_not_reported_as_fully_used(drawn):
    """The reported bug: an uncapped plan showed 0.00 GB / 0.00 GB at 100%."""
    _render(total_gb=0.0, days_left=None, status="pending_activation")
    blob = _blob(drawn)

    assert "0.00 GB / 0.00 GB" not in blob
    assert "100.0%" not in blob
    assert "Unlimited" in blob


def test_unlimited_traffic_ring_shows_infinity_not_a_percentage(drawn):
    _render(total_gb=0.0)
    blob = _blob(drawn)

    assert "∞" in blob
    assert "UNLIMITED" in blob
    assert "USED" not in blob


def test_metered_plan_still_shows_real_usage_and_percent(drawn):
    _render(used_gb=7.5, total_gb=10.0)
    blob = _blob(drawn)

    assert "7.50 GB / 10.00 GB" in blob
    assert "75.0%" in blob
    assert "USED" in blob
    assert "Unlimited" not in blob


def test_full_usage_still_reports_one_hundred_percent(drawn):
    """Guards the fix: only total_gb <= 0 is unlimited, 10/10 is genuinely full."""
    _render(used_gb=10.0, total_gb=10.0)
    blob = _blob(drawn)

    assert "100.0%" in blob
    assert "10.00 GB / 10.00 GB" in blob


def test_overusage_is_clamped_to_one_hundred_percent(drawn):
    _render(used_gb=25.0, total_gb=10.0)
    assert "100.0%" in _blob(drawn)


def test_no_expiry_renders_unlimited_instead_of_zero_days(drawn):
    _render(days_left=None)
    blob = _blob(drawn)

    assert "0 Days" not in blob
    assert "Unlimited ∞" in blob


def test_expires_today_still_renders_zero_days(drawn):
    """days_left=0 means "expires today" and must NOT be mistaken for unlimited."""
    _render(days_left=0)
    blob = _blob(drawn)

    assert "0 Days" in blob
    assert "Unlimited ∞" not in blob


def test_status_label_has_no_unrenderable_emoji(drawn):
    """Vazirmatn ships no colour-emoji glyphs, so these rendered as blank boxes."""
    _render(status="expired", is_active=False)
    blob = _blob(drawn)

    assert "🔴" not in blob
    assert "🟢" not in blob
    assert "EXPIRED" in blob


def test_pending_activation_is_not_labelled_active(drawn):
    """The caption says "activates on first connection", so ACTIVE contradicts it."""
    _render(status="pending_activation", is_active=True)
    blob = _blob(drawn)

    assert "READY" in blob
    assert "ACTIVE" not in blob


def test_long_config_name_is_truncated(drawn):
    _render(config_name="ب" * 80)
    header = next(t for t in drawn if "Config:" in t)
    assert len(header) < 90


def test_returns_png_bytes():
    out = _render()
    assert isinstance(out, io.BytesIO)
    assert out.getvalue().startswith(b"\x89PNG\r\n\x1a\n")


def test_renders_without_a_qr_uri():
    """vless_uri is empty for PasarGuard configs — must not crash."""
    out = _render(vless_uri=None)
    assert out.getvalue().startswith(b"\x89PNG\r\n\x1a\n")


def test_unlimited_on_both_axes(drawn):
    """The exact config from the report: no traffic cap and no expiry."""
    _render(total_gb=0.0, days_left=None, status="pending_activation")
    blob = _blob(drawn)

    assert "0.00 GB / Unlimited ∞" in blob
    assert "Unlimited ∞" in blob
    assert "0 Days" not in blob
    assert "100.0%" not in blob
