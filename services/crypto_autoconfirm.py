"""
Auto-confirm for manual crypto wallet payments.

The flow
========
When a user picks "manual crypto" topup, we add a small **unique suffix**
to the crypto amount (e.g. 10.0073 USDT instead of a clean 10.0000) and
ask the user to send EXACTLY that amount. A background worker job polls
each configured deposit address on TronGrid / TON-Center and, when it
sees an incoming transfer whose amount matches that exact figure and
whose timestamp is after the invoice was created, it confirms the
payment automatically — without the admin having to look at a TX hash.

Why this works
--------------
The pay_amount space is dense (5 decimals → 99 999 possible suffixes)
and our active invoice volume is small (≤ 100 at a time in practice),
so collision probability is well below 0.1%. We additionally narrow the
match by:
  * deposit address (only checks the wallet that issued the invoice)
  * earliest acceptable timestamp (the payment's created_at)
  * processed-tx-hash de-dup list stored on the payment row

Supported networks (best-effort, no API key required):
  * USDT-TRC20 / TRX  → https://api.trongrid.io
  * TON              → https://toncenter.com

Anything else (BTC, ETH, USDT-ERC20, etc.) falls back to the existing
manual hash-submission flow.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.payment import Payment

logger = logging.getLogger(__name__)


# Currencies for which we can auto-confirm. Anything else: admin still
# clicks "تأیید" by hand after the user posts a hash.
TRON_USDT_CURRENCIES: set[str] = {
    "USDT TRC20",
    "USDT-TRC20",
    "USDT (TRC20)",
}
TRX_CURRENCIES: set[str] = {"TRX", "TRON"}
TON_CURRENCIES: set[str] = {"TON"}

AUTOCONFIRM_CURRENCIES = TRON_USDT_CURRENCIES | TRX_CURRENCIES | TON_CURRENCIES


def is_autoconfirmable(currency: str | None) -> bool:
    if not currency:
        return False
    return currency.strip() in AUTOCONFIRM_CURRENCIES


# ─── 1. Unique amount with suffix ──────────────────────────────────────────
#
# Given a base crypto amount (e.g. 5.4321), we tack on a small random
# tail in the 5th + 6th decimal places. For USDT (which is shown to the
# user at 4 decimals on most wallets) we use the 5-6th decimals; for
# TRX/TON (6 decimals) we use the 6-7th. The user only sees the amount
# we tell them and is asked to send it exactly.

def quantize_for_currency(amount: Decimal, currency: str) -> Decimal:
    """Pick a sensible display precision per currency."""
    c = (currency or "").strip()
    if c in TRON_USDT_CURRENCIES:
        return amount.quantize(Decimal("0.000001"))   # 6 dp on USDT
    if c in TRX_CURRENCIES:
        return amount.quantize(Decimal("0.000001"))   # 6 dp on TRX
    if c in TON_CURRENCIES:
        return amount.quantize(Decimal("0.000000001"))  # 9 dp on TON
    return amount.quantize(Decimal("0.00000001"))


def generate_unique_pay_amount(
    base_amount: Decimal,
    currency: str,
    existing_pending_amounts: Iterable[Decimal] = (),
) -> Decimal:
    """Return ``base_amount`` plus a tiny random suffix that does NOT
    collide with any of ``existing_pending_amounts``.

    Strategy: try up to 50 random suffixes in the range [1, 9999] of the
    last meaningful unit. Suffix is always POSITIVE and < 1 cent so the
    USD value impact is negligible.
    """
    base = quantize_for_currency(base_amount, currency)
    pending = {quantize_for_currency(a, currency) for a in existing_pending_amounts}

    c = (currency or "").strip()
    # Suffix unit: how many digits below the integer part the suffix lives.
    if c in TRON_USDT_CURRENCIES or c in TRX_CURRENCIES:
        # Suffix in 10^-6..10^-4 range: e.g. 0.000123 → 0.0099%-level
        unit = Decimal("0.000001")
        max_suffix = 9999
    elif c in TON_CURRENCIES:
        unit = Decimal("0.000000001")
        max_suffix = 999_999
    else:
        unit = Decimal("0.00000001")
        max_suffix = 9999

    for _ in range(50):
        suffix = Decimal(random.randint(1, max_suffix)) * unit
        candidate = quantize_for_currency(base + suffix, currency)
        if candidate not in pending:
            return candidate
    # If we couldn't find a unique one (extremely unlikely), just return
    # base + small random — the autoconfirm watcher will be slightly more
    # conservative and the admin can still approve by hash.
    return quantize_for_currency(base + (Decimal(random.randint(1, max_suffix)) * unit), currency)


async def pending_amounts_for(
    session: AsyncSession,
    *,
    currency: str,
    address: str,
) -> list[Decimal]:
    """Existing pending invoice amounts for this exact (currency, address)
    pair — used to avoid suffix collisions."""
    result = await session.execute(
        select(Payment.callback_payload, Payment.pay_amount)
        .where(
            Payment.provider == "manual_crypto",
            Payment.payment_status.in_(("waiting_hash", "pending_approval", "waiting_receipt")),
            Payment.pay_currency == currency,
        )
    )
    out: list[Decimal] = []
    for payload, amt in result.all():
        if not isinstance(payload, dict):
            continue
        if payload.get("address") != address:
            continue
        crypto_amount = payload.get("crypto_amount")
        if crypto_amount is not None:
            try:
                out.append(Decimal(str(crypto_amount)))
            except Exception:
                pass
    return out


# ─── 2. Blockchain queries ─────────────────────────────────────────────────

# USDT TRC-20 contract on Tron mainnet.
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def fetch_tron_trc20_incoming(
    address: str,
    *,
    since: datetime,
    contract: str = USDT_TRC20_CONTRACT,
    timeout: float = 8.0,
) -> list[dict]:
    """Return incoming TRC-20 transfers to ``address`` newer than ``since``.

    Each item: {"hash": str, "amount": Decimal (token units), "timestamp": datetime}
    """
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
    params = {
        "only_confirmed": "true",
        "only_to": "true",
        "contract_address": contract,
        "min_timestamp": int(since.timestamp() * 1000),
        "limit": 50,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("TronGrid TRC20 fetch failed for %s: %s", address, exc)
        return []
    items: list[dict] = []
    for raw in data.get("data", []) or []:
        try:
            # USDT has 6 decimals.
            value = Decimal(str(raw.get("value"))) / Decimal(10 ** int(raw.get("token_info", {}).get("decimals", 6)))
            ts = datetime.fromtimestamp(int(raw.get("block_timestamp")) / 1000, tz=timezone.utc)
            items.append({"hash": raw.get("transaction_id"), "amount": value, "timestamp": ts})
        except Exception:
            continue
    return items


async def fetch_tron_trx_incoming(
    address: str,
    *,
    since: datetime,
    timeout: float = 8.0,
) -> list[dict]:
    """Native TRX transfers to ``address``."""
    url = f"https://api.trongrid.io/v1/accounts/{address}/transactions"
    params = {
        "only_confirmed": "true",
        "only_to": "true",
        "min_timestamp": int(since.timestamp() * 1000),
        "limit": 50,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("TronGrid TRX fetch failed for %s: %s", address, exc)
        return []
    items: list[dict] = []
    for raw in data.get("data", []) or []:
        try:
            contract = (raw.get("raw_data", {}).get("contract") or [{}])[0]
            if contract.get("type") != "TransferContract":
                continue
            param = contract.get("parameter", {}).get("value", {})
            value = Decimal(param.get("amount", 0)) / Decimal(10 ** 6)
            ts = datetime.fromtimestamp(int(raw.get("block_timestamp")) / 1000, tz=timezone.utc)
            items.append({"hash": raw.get("txID"), "amount": value, "timestamp": ts})
        except Exception:
            continue
    return items


async def fetch_ton_incoming(
    address: str,
    *,
    since: datetime,
    timeout: float = 8.0,
) -> list[dict]:
    """Incoming TON transfers to ``address`` via toncenter."""
    url = "https://toncenter.com/api/v2/getTransactions"
    params = {
        "address": address,
        "limit": 50,
        "to_lt": 0,
        "archival": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning("toncenter fetch failed for %s: %s", address, exc)
        return []
    items: list[dict] = []
    since_ts = int(since.timestamp())
    for raw in data.get("result", []) or []:
        try:
            utime = int(raw.get("utime", 0))
            if utime < since_ts:
                continue
            in_msg = raw.get("in_msg") or {}
            value = Decimal(in_msg.get("value", 0)) / Decimal(10 ** 9)
            if value <= 0:
                continue
            ts = datetime.fromtimestamp(utime, tz=timezone.utc)
            items.append({
                "hash": raw.get("transaction_id", {}).get("hash"),
                "amount": value,
                "timestamp": ts,
            })
        except Exception:
            continue
    return items


async def fetch_incoming(
    *,
    currency: str,
    address: str,
    since: datetime,
) -> list[dict]:
    """Dispatch to the right blockchain explorer based on currency."""
    if currency in TRON_USDT_CURRENCIES:
        return await fetch_tron_trc20_incoming(address, since=since)
    if currency in TRX_CURRENCIES:
        return await fetch_tron_trx_incoming(address, since=since)
    if currency in TON_CURRENCIES:
        return await fetch_ton_incoming(address, since=since)
    return []


# ─── 3. Amount-equality helper ─────────────────────────────────────────────
#
# Strict equality on Decimal with the quantize-for-currency precision.
# We deliberately do NOT use a fuzzy tolerance: the whole point of the
# suffix scheme is that an exact match is unique.

def amount_matches(currency: str, expected: Decimal, actual: Decimal) -> bool:
    q = quantize_for_currency(expected, currency)
    a = quantize_for_currency(actual, currency)
    return q == a
