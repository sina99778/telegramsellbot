from __future__ import annotations

from decimal import Decimal
from typing import Literal


def format_volume_bytes(volume_bytes: int) -> str:
    if volume_bytes is None or volume_bytes <= 0:
        return "0 GB"

    gigabytes = volume_bytes / (1024**3)
    if gigabytes.is_integer():
        return f"{int(gigabytes)} GB"
    return f"{gigabytes:.2f} GB"


def format_price(price: Decimal | float | str) -> str:
    """Format price to 2 decimal places, removing trailing zeros noise."""
    d = Decimal(str(price)).quantize(Decimal("0.01"))
    return f"{d:,.2f}"


def format_price_with_toman(price: Decimal | float | str, toman_rate: int) -> str:
    """Format price as USD + Toman equivalent."""
    d = Decimal(str(price)).quantize(Decimal("0.01"))
    toman = int(d * toman_rate)
    # Format toman with comma separator
    toman_str = f"{toman:,}"
    return f"{d:,.2f} USD (≈ {toman_str} تومان)"


# ─── Display-currency mode (new — drives unified user-facing prices) ────
#
# The bot stores every internal price/wallet balance in USD (Numeric(18,8)).
# The operator can choose, via `AppSettingsRepository.set_display_currency`,
# how those values are rendered TO CUSTOMERS:
#
#   mode="USD"  →  "12.50 $"
#   mode="IRT"  →  "2,187,500 تومان"
#
# Conversion uses the same rate the existing card-to-card / TetraPay
# flows already consume from `get_toman_rate()`. We never *store* Toman —
# we only render it on the way out, and accept it on the way in
# (`parse_money_input`).
DisplayCurrency = Literal["USD", "IRT"]


def format_money(
    usd_amount: Decimal | float | str,
    *,
    mode: DisplayCurrency = "USD",
    toman_rate: int = 100000,
) -> str:
    """Single source of truth for user-facing price strings.

    Pass USD amount + current display mode + current toman rate; you get
    back a Persian-friendly string. Use this everywhere a customer sees
    a price (wallet, plan, invoice, purchase confirmation). Admin-only
    audit views can keep using `format_price` directly for technical
    accuracy.
    """
    d = Decimal(str(usd_amount))
    if mode == "IRT":
        toman = int(d * toman_rate)
        return f"{toman:,} تومان"
    # Default USD
    return f"{d.quantize(Decimal('0.01')):,.2f} $"


def usd_to_toman(usd_amount: Decimal | float | str, toman_rate: int) -> int:
    """USD → integer Toman (no decimals; Toman has no sub-unit in practice)."""
    d = Decimal(str(usd_amount))
    return int(d * toman_rate)


def toman_to_usd(toman_amount: int | Decimal, toman_rate: int) -> Decimal:
    """Toman → USD as Decimal, rounded to 2 dp (cent-precision).

    Used when a customer asks to top up with Toman and we credit USD,
    or when importing wallets from a Toman-only ledger.
    """
    if toman_rate <= 0:
        return Decimal("0.00")
    return (Decimal(str(toman_amount)) / Decimal(toman_rate)).quantize(Decimal("0.01"))


def parse_money_input(
    raw: str,
    *,
    mode: DisplayCurrency,
    toman_rate: int,
) -> Decimal:
    """Parse what a customer typed in a topup-amount input field.

    Honours the current display mode:
      mode=USD →  "12.50" → Decimal("12.50")
      mode=IRT →  "2,187,500" / "۲٫۱۸۷٫۵۰۰" → Decimal("12.50")  (rate-converted)

    Strips Persian commas (٫), Arabic commas (،), Latin commas, and
    Persian digits (۰-۹). Always returns USD Decimal so the rest of the
    pipeline doesn't have to think about mode.
    """
    if not raw:
        raise ValueError("empty amount")
    # Persian digits → Latin
    s = raw.strip().translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٫،,٬", "0123456789...."))
    # Remove all decimal/thousands separators we just normalised
    s = s.replace(",", "").replace(".", "", s.count(".") - 1 if s.count(".") > 1 else 0)
    # Allow a single "." as decimal point
    try:
        amount = Decimal(s)
    except Exception as exc:
        raise ValueError(f"invalid amount: {raw!r}") from exc
    if amount <= 0:
        raise ValueError("amount must be positive")
    if mode == "IRT":
        return toman_to_usd(int(amount), toman_rate)
    return amount.quantize(Decimal("0.01"))


def escape_markdown(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


def format_usage_bar(used: int, total: int, width: int = 10) -> str:
    """Create a text-based progress bar for usage display."""
    if total <= 0:
        return "▓" * width
    ratio = min(used / total, 1.0)
    filled = round(ratio * width)
    empty = width - filled
    bar = "▓" * filled + "░" * empty
    pct = round(ratio * 100)
    return f"{bar} {pct}%"
