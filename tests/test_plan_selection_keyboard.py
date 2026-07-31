"""Tests for the plan-selection keyboard (apps.bot.keyboards.inline).

This keyboard is the storefront: it is the first thing a buyer sees and the only
place plans are ranked. Two rules matter enough to pin down in CI:

1. Unlimited plans are a *different product*, not "an expensive GB plan", so they
   must never be interleaved into the price ladder. Before this was enforced, a
   245,000 unlimited plan sorted between the 40GB (239,999) and 50GB (299,999)
   plans and read as just another rung on the same ladder.
2. Unlimited plans must be visually distinct. They store ``volume_bytes = 0``,
   which used to make the volume bit vanish entirely — the button rendered as
   ``name — 1 ماه • 245,000`` with a blank where the size belonged, i.e. it
   looked like a metered plan whose size failed to render.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from apps.bot.keyboards.inline import build_plan_selection_keyboard


class _Plan:
    """Duck-typed stand-in for models.plan.Plan.

    The builder only reads id/name/price/currency/volume_bytes/duration_days, so
    a stub keeps these tests free of a DB session and of SQLAlchemy state.
    """

    def __init__(self, name, price, volume_bytes, duration_days=30, currency="USD"):
        self.id = uuid4()
        self.name = name
        self.price = Decimal(str(price))
        self.currency = currency
        self.volume_bytes = volume_bytes
        self.duration_days = duration_days


GB = 1024 ** 3


def _plan_rows(markup):
    """Button texts for plan rows only, in render order.

    Filters out the custom-purchase and cancel buttons so assertions describe
    the plan ladder itself rather than the surrounding chrome.
    """
    return [
        btn.text
        for row in markup.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith("plan:select:")
    ]


# The exact ladder from the reported screenshot, already price-sorted by the
# caller's ORDER BY, with the unlimited plan sitting in the middle.
def _screenshot_plans():
    return [
        _Plan("10 گیگ یکماهه", "59.999", 10 * GB),
        _Plan("20 گیگ یکماهه", "120.000", 20 * GB),
        _Plan("30 گیگ یکماهه", "180.000", 30 * GB),
        _Plan("40 گیگ یکماهه", "239.999", 40 * GB),
        _Plan("نامحدود یکماهه 2 کاربر", "245.000", 0),
        _Plan("50 گیگ یکماهه", "299.999", 50 * GB),
    ]


def test_unlimited_plan_is_pinned_last_despite_its_price():
    rows = _plan_rows(build_plan_selection_keyboard(_screenshot_plans()))
    assert "نامحدود یکماهه 2 کاربر" in rows[-1]


def test_metered_plans_keep_the_callers_price_order():
    rows = _plan_rows(build_plan_selection_keyboard(_screenshot_plans()))
    metered = [r for r in rows if "گیگ" in r]
    assert [r.split(" —")[0].lstrip("⭐💎 ") for r in metered] == [
        "10 گیگ یکماهه",
        "20 گیگ یکماهه",
        "30 گیگ یکماهه",
        "40 گیگ یکماهه",
        "50 گیگ یکماهه",
    ]


def test_unlimited_plan_is_visually_distinct():
    rows = _plan_rows(build_plan_selection_keyboard(_screenshot_plans()))
    unlimited = rows[-1]
    # Its own badge, plus a real volume label instead of a silent gap.
    assert unlimited.startswith("💎 ")
    assert "نامحدود ♾" in unlimited


def test_recommended_badge_never_lands_on_an_unlimited_plan():
    # An unlimited plan's price-per-day always wins, so without an explicit
    # exclusion the ⭐ badge would permanently stick to it and mean nothing.
    plans = [
        _Plan("cheap unlimited", "1.00", 0, duration_days=30),
        _Plan("10GB", "5.00", 10 * GB, duration_days=30),
        _Plan("20GB", "8.00", 20 * GB, duration_days=30),
    ]
    rows = _plan_rows(build_plan_selection_keyboard(plans))
    starred = [r for r in rows if r.startswith("⭐ ")]
    assert len(starred) == 1
    assert "unlimited" not in starred[0]


def test_all_unlimited_catalogue_still_renders():
    # Guards the `ranked = metered or plan_list` fallback: with no metered plans
    # the ranking list would otherwise be empty.
    plans = [_Plan("unlimited A", "10.00", 0), _Plan("unlimited B", "20.00", 0)]
    rows = _plan_rows(build_plan_selection_keyboard(plans))
    assert len(rows) == 2
    assert all(r.startswith("💎 ") for r in rows)


def test_accepts_a_generator_of_plans():
    # The parameter is typed Iterable[Plan]; partitioning must not consume the
    # input twice, which would silently drop every unlimited plan.
    plans = _screenshot_plans()
    rows = _plan_rows(build_plan_selection_keyboard(p for p in plans))
    assert len(rows) == len(plans)
    assert "نامحدود ♾" in rows[-1]


def test_metered_plans_do_not_get_the_unlimited_label():
    rows = _plan_rows(build_plan_selection_keyboard(_screenshot_plans()))
    for row in rows[:-1]:
        assert "نامحدود ♾" not in row
        assert not row.startswith("💎 ")


def test_cancel_button_stays_after_the_plan_rows():
    markup = build_plan_selection_keyboard(_screenshot_plans())
    texts = [btn.text for row in markup.inline_keyboard for btn in row]
    assert texts[-1] == "❌ انصراف"
