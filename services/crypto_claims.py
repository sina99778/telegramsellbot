from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from models.crypto_claim import CryptoTransactionClaim


def currency_key(currency: str) -> str:
    value = currency.strip().upper()
    if value in {"TRX", "TRON"}:
        return "TRX"
    if value in {"USDT TRC20", "USDT-TRC20", "USDT (TRC20)"}:
        return "USDT TRC20"
    return value


def transaction_key(currency: str, tx_hash: str) -> tuple[str, str]:
    value = currency_key(currency)
    chain = "TRON" if value in {"TRX", "USDT TRC20"} else value
    if value in {"ETH", "ETHEREUM", "USDT ERC20", "USDT-ERC20", "USDT (ERC20)", "USDC ERC20"}:
        chain = "ETHEREUM"
    if value in {"BTC", "BITCOIN"}:
        chain = "BITCOIN"
    if value in {"BNB", "USDT BSC", "USDT BEP20", "USDT-BEP20"}:
        chain = "BSC"
    if value in {"LTC", "LITECOIN"}:
        chain = "LITECOIN"
    if value in {"DOGE", "DOGECOIN"}:
        chain = "DOGECOIN"
    if value in {"XRP", "RIPPLE"}:
        chain = "XRP"
    value_hash = tx_hash.strip()
    if chain in {"TRON", "ETHEREUM", "BITCOIN", "BSC", "LITECOIN", "DOGECOIN", "XRP"}:
        value_hash = value_hash.lower()
    if chain == "TON":
        # TON explorers expose the same 32-byte identity in hex and base64.
        import base64
        import re
        if re.fullmatch(r"[0-9a-fA-F]{64}", value_hash):
            value_hash = value_hash.lower()
        else:
            try:
                decoded = base64.b64decode(value_hash + "="*((-len(value_hash))%4), altchars=b"-_", validate=True)
                if len(decoded) == 32:
                    value_hash = decoded.hex()
            except ValueError:
                pass
    return chain, value_hash


async def claim_crypto_transaction(session, payment) -> None:
    payload = payment.callback_payload or {}
    tx_hash = payload.get("tx_hash") or payment.provider_payment_id
    if not isinstance(tx_hash, str) or not tx_hash.strip():
        raise ValueError("A verified transaction hash is required for a crypto payment.")
    chain, tx_hash = transaction_key(payment.pay_currency or "Crypto", tx_hash)
    if len(chain) > 32 or len(tx_hash) > 200:
        raise ValueError("Invalid crypto transaction identity.")
    stmt = insert(CryptoTransactionClaim).values(
        chain=chain, tx_hash=tx_hash, payment_id=payment.id,
    ).on_conflict_do_nothing(index_elements=["chain", "tx_hash"]).returning(CryptoTransactionClaim.payment_id)
    claimed = await session.scalar(stmt)
    if claimed is not None:
        return
    owner = await session.scalar(select(CryptoTransactionClaim.payment_id).where(
        CryptoTransactionClaim.chain == chain, CryptoTransactionClaim.tx_hash == tx_hash,
    ))
    if owner != payment.id:
        raise ValueError("This blockchain transaction has already funded another payment.")
