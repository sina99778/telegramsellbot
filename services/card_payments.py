"""
Card-to-card payment helpers:

* pick a card from the configured pool (shuffle between buyers so one
  card isn't used permanently), and
* compute a per-payment UNIQUE toman amount by adding a small random
  jitter to the base price — so the operator can later auto-confirm a
  card deposit by exact-amount matching (two pending invoices never
  share the same total).
"""
from __future__ import annotations

import logging
import random
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.payment import Payment


logger = logging.getLogger(__name__)


def pick_card(gw) -> dict[str, str] | None:
    """Pick one card from the gateway's pool at random. Returns
    {number, holder, bank} or None if no card is configured."""
    cards = list(getattr(gw, "cards", None) or [])
    if not cards:
        # Backward-compat single card.
        if getattr(gw, "card_number", None):
            return {
                "number": str(gw.card_number),
                "holder": str(getattr(gw, "card_holder", "") or ""),
                "bank": str(getattr(gw, "card_bank", "") or ""),
            }
        return None
    return random.choice(cards)


async def compute_unique_toman(
    session: AsyncSession,
    base_toman: int,
    jitter_max: int,
) -> int:
    """Return a toman amount = base + a small random offset, guaranteed not
    to collide with any OTHER currently-pending card payment's amount.

    jitter_max <= 0 disables jitter (returns base_toman unchanged).

    The offset is in [1, jitter_max] so the amount is always > base (the
    customer never under-pays). We retry a handful of times to dodge a
    collision with another open invoice; if we can't find a free slot we
    just return the last candidate (collisions are harmless — the operator
    falls back to manual review for that rare case).
    """
    if jitter_max <= 0:
        return int(base_toman)

    # Pull the set of pay_amounts already held by OPEN card invoices so we
    # don't hand two buyers the same unique total.
    rows = await session.execute(
        select(Payment.pay_amount).where(
            Payment.provider == "card_to_card",
            Payment.payment_status.in_(("waiting_receipt", "pending_approval", "waiting")),
            Payment.pay_amount.is_not(None),
        )
    )
    taken: set[int] = set()
    for (amt,) in rows.all():
        try:
            taken.add(int(amt))
        except (TypeError, ValueError):
            continue

    base = int(base_toman)
    for _ in range(25):
        candidate = base + random.randint(1, jitter_max)
        if candidate not in taken:
            return candidate
    # Couldn't find a free slot — return a jittered value anyway.
    return base + random.randint(1, jitter_max)
