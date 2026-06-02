"""Tests for renewal price math (services/renewal.calculate_renewal_price).

This is the money path used by manual renewal AND the new auto-renew job, so a
regression here mischarges customers — exactly what CI should catch.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from repositories.settings import RenewalSettings
from services.renewal import calculate_renewal_price

# 0.10 $/GB ; 1.00 $/10-days  → 0.10 $/day
S = RenewalSettings(price_per_gb=0.1, price_per_10_days=1.0)


def test_volume_global_rate():
    assert calculate_renewal_price(renew_type="volume", amount=50, settings=S) == Decimal("5.00")


def test_time_global_rate():
    # per_day = price_per_10_days / 10 = 0.10 ; 30 × 0.10 = 3.00
    assert calculate_renewal_price(renew_type="time", amount=30, settings=S) == Decimal("3.00")


def test_volume_default_override():
    assert calculate_renewal_price(
        renew_type="volume", amount=10, settings=S, default_per_gb=0.2
    ) == Decimal("2.00")


def test_time_default_override():
    assert calculate_renewal_price(
        renew_type="time", amount=10, settings=S, default_per_day=0.5
    ) == Decimal("5.00")


def test_result_is_rounded_to_cents():
    # 7 × 0.10 = 0.70
    assert calculate_renewal_price(renew_type="time", amount=7, settings=S) == Decimal("0.70")


def test_non_positive_amount_raises():
    with pytest.raises(ValueError):
        calculate_renewal_price(renew_type="time", amount=0, settings=S)
    with pytest.raises(ValueError):
        calculate_renewal_price(renew_type="volume", amount=-5, settings=S)


def test_invalid_renew_type_raises():
    with pytest.raises(ValueError):
        calculate_renewal_price(renew_type="bogus", amount=5, settings=S)
