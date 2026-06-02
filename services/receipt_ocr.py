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
        logger.info("pytesseract/tesseract not installed — receipt OCR skipped")
        return None
    try:
        img = Image.open(io.BytesIO(blob))
        # Upscale small screenshots a bit — helps tesseract on thin digits.
        if img.width < 1000:
            scale = 1000 / max(img.width, 1)
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        return pytesseract.image_to_string(img, lang="fas+eng")
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR failed: %s", exc)
        return None


async def ocr_receipt_text(file_id: str) -> str | None:
    blob = await _download_telegram_file(file_id)
    if not blob:
        return None
    return await asyncio.to_thread(_ocr_bytes, blob)


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
    """Download + OCR + assess in one call. Never raises."""
    try:
        text = await ocr_receipt_text(file_id)
        return assess_receipt(
            text, card_number=card_number, card_holder=card_holder, expected_toman=expected_toman
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("receipt assessment failed: %s", exc)
        return {"ok": None, "summary": "🧾 OCR: بررسی خودکار ناموفق — دستی بررسی کن."}
