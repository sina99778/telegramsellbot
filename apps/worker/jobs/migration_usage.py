"""
Worker job: backfill the "used volume" for configs that were migrated from the
legacy bot to a real inbound BEFORE the usage-subtraction fix landed.

The problem
-----------
When an imported (legacy) config was migrated to one of the operator's inbounds,
the bot provisioned its FULL purchased volume — even if the user had already
consumed part of it on their old client. So a user who had used 10 of 30 GB got
a brand-new 30 GB: free traffic for them, a loss for the operator.

What this does
--------------
For each migrated config that hasn't been reconciled yet, it reads the OLD
client's real consumption from the panel (one get_inbounds per server, matched
by email == legacy_remark on a different inbound than the new client) and
reduces BOTH the panel quota and the DB volume to the true remaining. It's:

* idempotent  — a per-sub `migration_usage_reconciled` flag, set once;
* bounded     — at most `BATCH_PER_SERVER` subs per server per run;
* cheap when idle — returns immediately if nothing is pending (no panel call);
* self-healing — clears the backlog over a few cycles, then becomes a no-op.

Runs on a timer from apps/worker/main.py.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from core.database import AsyncSessionFactory
from models.subscription import Subscription
from models.xui import XUIServerRecord
from services.provisioning.manager import ProvisioningManager

logger = logging.getLogger(__name__)

BATCH_PER_SERVER = 200


async def run_reconcile_migrated_usage() -> None:
    # Top-level isolation so a failure in the SETUP phase (session creation, the
    # pending-count gate, the server query) can't escape to APScheduler — the
    # per-server loop below is already guarded, but the setup before it was not.
    # Mirrors the run_* error-isolated wrappers in apps/worker/main.py.
    try:
        await _reconcile_migrated_usage()
    except Exception as exc:  # noqa: BLE001
        logger.error("[MIGRATE-USAGE] run failed: %s", exc, exc_info=True)


async def _reconcile_migrated_usage() -> None:
    async with AsyncSessionFactory() as session:
        # Cheap gate: is there ANY migrated config still needing reconciliation?
        # If not, skip entirely so we never hit the panel for nothing.
        pending = await session.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.source.is_(None),
                Subscription.legacy_remark.isnot(None),
                Subscription.migration_usage_reconciled.is_(False),
            )
        )
        if not pending:
            return

        logger.info("[MIGRATE-USAGE] %d migrated config(s) pending usage reconciliation", pending)

        servers = list(
            (
                await session.execute(
                    select(XUIServerRecord)
                    # credentials eager-loaded: the manager builds the panel
                    # client from server.credentials (a relationship) — a lazy
                    # load there would raise greenlet_spawn in this async path.
                    .options(selectinload(XUIServerRecord.credentials))
                    .where(XUIServerRecord.is_active.is_(True))
                )
            ).scalars().all()
        )

        manager = ProvisioningManager(session)
        total = {"checked": 0, "fixed": 0, "no_data": 0}
        for server in servers:
            try:
                res = await manager.reconcile_migrated_usage_for_server(
                    server, limit=BATCH_PER_SERVER
                )
                # Commit each server's batch so progress is durable even if a
                # later server errors.
                await session.commit()
                for k in total:
                    total[k] += res.get(k, 0)
            except Exception as exc:
                await session.rollback()
                logger.error(
                    "[MIGRATE-USAGE] reconciliation failed for server %s: %s",
                    getattr(server, "name", server.id), exc, exc_info=True,
                )

        if total["checked"]:
            logger.info(
                "[MIGRATE-USAGE] run complete — checked=%d, fixed=%d, no_legacy_data=%d",
                total["checked"], total["fixed"], total["no_data"],
            )
