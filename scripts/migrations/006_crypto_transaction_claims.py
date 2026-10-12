"""Seed permanent replay protection from previously credited crypto payments.

Existing duplicates are retained for reconciliation; their hashes are blocked
from future use. No historical financial records are deleted or modified.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from core.database import AsyncSessionFactory, engine
from models.crypto_claim import CryptoTransactionClaim
from models.payment import Payment
from services.crypto_claims import transaction_key


async def migrate() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(CryptoTransactionClaim.__table__.create, checkfirst=True)
    async with AsyncSessionFactory() as session:
        rows = (await session.execute(select(Payment).where(
            Payment.provider == "manual_crypto", Payment.actually_paid.is_not(None),
        ).order_by(Payment.created_at))).scalars().all()
        for payment in rows:
            tx_hash = (payment.callback_payload or {}).get("tx_hash") or payment.provider_payment_id
            if not isinstance(tx_hash, str) or not tx_hash.strip():
                logging.warning("Historical credited crypto payment %s has no hash; review manually", payment.id)
                continue
            chain, value = transaction_key(payment.pay_currency or "Crypto", tx_hash)
            result = await session.scalar(insert(CryptoTransactionClaim).values(
                chain=chain, tx_hash=value, payment_id=payment.id,
            ).on_conflict_do_nothing(index_elements=["chain", "tx_hash"]).returning(CryptoTransactionClaim.payment_id))
            if result is None:
                owner = await session.scalar(select(CryptoTransactionClaim.payment_id).where(
                    CryptoTransactionClaim.chain == chain, CryptoTransactionClaim.tx_hash == value,
                ))
                if owner != payment.id:
                    logging.warning("Historical crypto replay on payment %s; reconcile ledger manually", payment.id)
        await session.commit()


async def run() -> None:
    try:
        await migrate()
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except Exception:
        logging.exception("Crypto transaction claim migration failed")
        sys.exit(1)
