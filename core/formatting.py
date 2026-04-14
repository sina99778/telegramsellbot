from __future__ import annotations

from decimal import Decimal


def format_volume_bytes(volume_bytes: int) -> str:
    if volume_bytes <= 0:
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
