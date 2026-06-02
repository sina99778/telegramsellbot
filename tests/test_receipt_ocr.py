"""Unit tests for the card-receipt fraud assist (services/receipt_ocr.py).

Pure-logic tests — they feed mock OCR text into assess_receipt() and check the
verdict, so no tesseract binary or network is needed.
"""
from __future__ import annotations

from services.receipt_ocr import (
    _digits_only,
    _name_matches,
    _norm_fa,
    _number_matches,
    assess_receipt,
    hamming_hex,
)

CARD = "6037997412345678"
HOLDER = "سینا نجفی"


# ── verdict: legitimate receipts ──────────────────────────────────────────


def test_legit_full_card_number():
    v = assess_receipt(
        "واریز به کارت 6037997412345678 مبلغ 50000",
        card_number=CARD, card_holder=HOLDER,
    )
    assert v["ok"] is True


def test_legit_masked_card_first6_last4():
    # Iranian receipts often mask the middle: first-6 + last-4 still visible.
    v = assess_receipt(
        "کارت مقصد 603799******5678",
        card_number=CARD, card_holder=HOLDER,
    )
    assert v["ok"] is True


def test_legit_name_only():
    v = assess_receipt("به نام سینا نجفی", card_number=CARD, card_holder=HOLDER)
    assert v["ok"] is True


def test_legit_name_fuzzy_ocr_garble():
    # OCR misread one character (نجفی → نجغی) — fuzzy match still passes.
    v = assess_receipt("بنام سینا نجغی", card_number=CARD, card_holder=HOLDER)
    assert v["ok"] is True


def test_persian_digits_normalised():
    v = assess_receipt("کارت ۶۰۳۷۹۹۷۴۱۲۳۴۵۶۷۸", card_number=CARD, card_holder=HOLDER)
    assert v["ok"] is True


# ── verdict: fraud (paid someone else) ────────────────────────────────────


def test_fraud_different_card_and_name():
    v = assess_receipt(
        "واریز به علی رضایی کارت 5022291011112222",
        card_number=CARD, card_holder=HOLDER,
    )
    assert v["ok"] is False
    assert "پیدا نشد" in v["summary"]


def test_fraud_empty_text_is_inconclusive():
    v = assess_receipt("", card_number=CARD, card_holder=HOLDER)
    assert v["ok"] is None  # couldn't read → review manually, not a 🚩


def test_none_text_is_inconclusive():
    v = assess_receipt(None, card_number=CARD, card_holder=HOLDER)
    assert v["ok"] is None


# ── amount note ───────────────────────────────────────────────────────────


def test_amount_match_note():
    v = assess_receipt(
        "کارت 6037997412345678 مبلغ 512,000 تومان",
        card_number=CARD, card_holder=HOLDER, expected_toman=512000,
    )
    assert "مبلغ ✅" in v["summary"]


def test_amount_mismatch_note():
    v = assess_receipt(
        "کارت 6037997412345678 مبلغ 999",
        card_number=CARD, card_holder=HOLDER, expected_toman=512000,
    )
    assert "مبلغ ❓" in v["summary"]


# ── low-level helpers ─────────────────────────────────────────────────────


def test_number_matches_masked():
    assert _number_matches(CARD, _digits_only("603799 ** 5678")) is True


def test_number_matches_rejects_other():
    assert _number_matches(CARD, _digits_only("5022291011112222")) is False


def test_name_matches_fuzzy():
    assert _name_matches(HOLDER, "بنام سینا نجغی") is True
    assert _name_matches(HOLDER, "علی رضایی") is False


def test_norm_fa_unifies_ya_kaf():
    # Arabic ي/ك should normalise to Persian ی/ک so matching is stable.
    assert _norm_fa("كيف") == _norm_fa("کیف")


def test_hamming_hex():
    assert hamming_hex("0000000000000000", "0000000000000000") == 0
    assert hamming_hex("ffffffffffffffff", "0000000000000000") == 64
    assert hamming_hex(None, "abc") == 999  # null-safe
    assert hamming_hex("zz", "00") == 999   # non-hex safe
