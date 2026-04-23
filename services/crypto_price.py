"""
Crypto price conversion service.
Uses CoinGecko free API to fetch real-time USD prices for major cryptocurrencies.
"""
from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger(__name__)

# Map our internal currency names to CoinGecko IDs
_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "LTC": "litecoin",
    "TRX": "tron",
    "TON": "the-open-network",
    "USDT TRC20": "tether",
    "USDT ERC20": "tether",
    "USDT": "tether",
    "DOGE": "dogecoin",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
}

# Simple in-memory cache: {coingecko_id: (price_usd, timestamp)}
_price_cache: dict[str, tuple[Decimal, float]] = {}
_CACHE_TTL_SECONDS = 120  # 2 minutes


async def get_crypto_price_usd(currency: str) -> Decimal | None:
    """
    Get the current USD price of a cryptocurrency.
    Returns None if the price cannot be fetched.
    """
    # Normalize currency name
    currency_upper = currency.strip().upper()
    
    # Find CoinGecko ID
    coin_id = _COINGECKO_IDS.get(currency_upper)
    if coin_id is None:
        # Try partial match
        for key, cid in _COINGECKO_IDS.items():
            if key in currency_upper or currency_upper in key:
                coin_id = cid
                break
    
    if coin_id is None:
        logger.warning("Unknown crypto currency for price lookup: %s", currency)
        return None

    # Check cache
    cached = _price_cache.get(coin_id)
    if cached is not None:
        price, ts = cached
        if time.time() - ts < _CACHE_TTL_SECONDS:
            return price

    # Fetch from CoinGecko
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": coin_id, "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            
        price_raw = data.get(coin_id, {}).get("usd")
        if price_raw is None:
            logger.warning("CoinGecko returned no price for %s", coin_id)
            return None
            
        price = Decimal(str(price_raw))
        _price_cache[coin_id] = (price, time.time())
        return price
        
    except (httpx.HTTPError, InvalidOperation, KeyError, TypeError) as exc:
        logger.warning("Failed to fetch crypto price for %s: %s", currency, exc)
        # Return cached value even if stale
        if cached is not None:
            return cached[0]
        return None


async def convert_usd_to_crypto(usd_amount: Decimal, currency: str) -> tuple[Decimal | None, Decimal | None]:
    """
    Convert a USD amount to a crypto amount.
    
    Returns:
        (crypto_amount, price_per_unit) or (None, None) if conversion fails.
    """
    price = await get_crypto_price_usd(currency)
    if price is None or price <= 0:
        return None, None
    
    crypto_amount = usd_amount / price
    return crypto_amount, price
