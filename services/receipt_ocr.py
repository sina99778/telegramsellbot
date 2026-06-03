"""
Card-receipt OCR fraud assist.

Some users send a screenshot of a transfer they made to SOMEONE ELSE, hoping
the operator approves it. This reads the receipt image and checks whether the
operator's OWN card number / holder name actually appears on it.

It is an ASSIST, not an auto-decision: the operator still approves/rejects.
Card NUMBERS drive the verdict because digits OCR reliably (and Iranian receipts
show the destination card's first-6 + last-4 even when masked); the holder name
is a fuzzy secondary signal. A 🔴 only fires when NEITHER the operator's card
number NOR name is found — so a legitimate receipt (whose digits read fine)
won't be falsely flagged.

Everything degrades gracefully: if tesseract/pytesseract isn't installed or the
image can't be read, the verdict is "couldn't read — review manually" and the
approval flow is never blocked.
"""
from __future__ import annotations

import asyncio
import difflib
import io
import logging
import re

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm_digits(text: str) -> str:
    return (text or "").translate(_FA_DIGITS)


def _digits_only(text: str) -> str:
    return re.sub(r"\D", "", _norm_digits(text))


def _norm_fa(text: str) -> str:
    """Normalise Persian text for matching: unify ی/ک, drop ZWNJ/marks, collapse spaces."""
    s = _norm_digits(text or "")
    s = (
        s.replace("ي", "ی").replace("ك", "ک")
        .replace("‌", " ").replace("‏", "").replace("‎", "")
        .replace("ي", "ی").replace("ك", "ک")
    )
    s = re.sub(r"[^\w؀-ۿ\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


async def _download_telegram_file(file_id: str) -> bytes | None:
    token = settings.bot_token.get_secret_value()
    if not token or token == "CHANGE_ME":
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{token}/getFile", params={"file_id": file_id}
            )
            r.raise_for_status()
            path = (r.json() or {}).get("result", {}).get("file_path")
            if not path:
                return None
            fr = await client.get(f"https://api.telegram.org/file/bot{token}/{path}")
            fr.raise_for_status()
            return fr.content
    except Exception as exc:  # noqa: BLE001
        logger.warning("receipt download failed: %s", exc)
        return None


def _ocr_bytes(blob: bytes) -> str | None:
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("receipt OCR: pytesseract is not installed")
        return None
    try:
        img = Image.open(io.BytesIO(blob))
        # Upscale small screenshots a bit — helps tesseract on thin digits.
        if img.width < 1000:
            scale = 1000 / max(img.width, 1)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("receipt OCR: cannot open image: %s", exc)
        return None
    # Try Persian+English, then English alone (still reads the card DIGITS — the
    # main fraud signal — when the 'fas' language pack is missing), then default.
    for lang in ("fas+eng", "eng", None):
        try:
            text = (
                pytesseract.image_to_string(img, lang=lang)
                if lang
                else pytesseract.image_to_string(img)
            )
            if text and text.strip():
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("receipt OCR failed (lang=%s): %s", lang, exc)
    return None


def ocr_status() -> dict:
    """Diagnose the OCR engine for the admin 'test OCR' button."""
    try:
        import pytesseract
    except ImportError:
        return {"available": False, "reason": "کتابخانه‌ی pytesseract نصب نیست."}
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"باینریِ tesseract پیدا/اجرا نشد: {str(exc)[:160]}"}
    try:
        langs = list(pytesseract.get_languages(config=""))
    except Exception:  # noqa: BLE001
        langs = []
    has_fas = "fas" in langs
    return {
        "available": True,
        "version": version,
        "langs": langs,
        "has_fas": has_fas,
        "reason": "آماده ✅" if has_fas else "آماده، ولی زبانِ fas نصب نیست — فقط ارقام خوانده می‌شود ⚠️",
    }


async def ocr_receipt_text(file_id: str) -> str | None:
    blob = await _download_telegram_file(file_id)
    if not blob:
        return None
    return await asyncio.to_thread(_ocr_bytes, blob)


def _dhash_bytes(blob: bytes) -> str | None:
    """64-bit perceptual difference-hash (hex) — catches the SAME receipt even
    if re-screenshotted/recompressed. Uses Pillow only (no extra dependency)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(io.BytesIO(blob)).convert("L").resize((9, 8))
        px = list(img.getdata())  # 8 rows × 9 cols, row-major
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
        return f"{bits:016x}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("dhash failed: %s", exc)
        return None


def hamming_hex(a: str | None, b: str | None) -> int:
    """Bit difference between two 16-hex-char hashes (999 = incomparable)."""
    if not a or not b:
        return 999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 999


def _name_matches(holder: str, ocr_text: str) -> bool:
    holder_n = _norm_fa(holder)
    text_n = _norm_fa(ocr_text)
    if not holder_n or not text_n:
        return False
    tokens = [t for t in holder_n.split() if len(t) >= 2]
    if not tokens:
        return False
    words = text_n.split()
    hits = 0
    for tok in tokens:
        if tok in text_n:
            hits += 1
            continue
        # fuzzy: tolerate OCR garbling a couple of chars
        if difflib.get_close_matches(tok, words, n=1, cutoff=0.78):
            hits += 1
    # Require a majority of the name's tokens to be present.
    return hits >= max(1, (len(tokens) + 1) // 2)


def _number_matches(card_number: str, ocr_digits: str) -> bool:
    num = _digits_only(card_number)
    if len(num) < 12 or not ocr_digits:
        return False
    first6, last4 = num[:6], num[-4:]
    # Full match, or the visible-when-masked first-6 AND last-4 both present.
    return num in ocr_digits or (first6 in ocr_digits and last4 in ocr_digits)


def assess_receipt(
    ocr_text: str | None,
    *,
    card_number: str | None,
    card_holder: str | None,
    expected_toman: int | None = None,
) -> dict:
    """Return a verdict dict: {ok: True/False/None, summary: str}."""
    if not ocr_text or not ocr_text.strip():
        return {"ok": None, "summary": "🧾 OCR: متن رسید خوانده نشد — دستی بررسی کن."}

    ocr_digits = _digits_only(ocr_text)
    num_found = _number_matches(card_number or "", ocr_digits)
    name_found = _name_matches(card_holder or "", ocr_text)

    amount_note = ""
    if expected_toman:
        amt = str(int(expected_toman))
        if amt in ocr_digits or amt[:-1] in ocr_digits:  # tolerate trailing-digit rounding
            amount_note = " | مبلغ ✅"
        else:
            amount_note = " | مبلغ ❓"

    if num_found or name_found:
        sig = []
        if num_found:
            sig.append("شماره‌کارت")
        if name_found:
            sig.append("نام")
        return {
            "ok": True,
            "summary": f"🧾 OCR: گیرنده با کارتِ تو می‌خوانَد ({'+'.join(sig)} پیدا شد){amount_note}",
        }
    return {
        "ok": False,
        "summary": (
            "🚩 <b>OCR: نام/شماره‌ی کارتِ تو در این رسید پیدا نشد!</b>"
            f" احتمالِ واریز به حسابِ دیگری.{amount_note}"
        ),
    }


async def assess_card_receipt(
    file_id: str,
    *,
    card_number: str | None,
    card_holder: str | None,
    expected_toman: int | None = None,
) -> dict:
    """Download once, then OCR + perceptual-hash + assess. Never raises.
    Returns the verdict dict plus a "phash" (for duplicate-receipt detection)."""
    try:
        blob = await _download_telegram_file(file_id)
        if not blob:
            return {"ok": None, "summary": "🧾 OCR: متن رسید خوانده نشد — دستی بررسی کن.", "phash": None}
        text = await asyncio.to_thread(_ocr_bytes, blob)
        phash = _dhash_bytes(blob)
        verdict = assess_receipt(
            text, card_number=card_number, card_holder=card_holder, expected_toman=expected_toman
        )
        verdict["phash"] = phash
        return verdict
    except Exception as exc:  # noqa: BLE001
        logger.warning("receipt assessment failed: %s", exc)
        return {"ok": None, "summary": "🧾 OCR: بررسی خودکار ناموفق — دستی بررسی کن.", "phash": None}
