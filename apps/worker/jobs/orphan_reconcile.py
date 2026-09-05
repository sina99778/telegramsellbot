"""Retry the compensating delete for panel clients we may have orphaned.

When a panel write's outcome is UNCERTAIN (a mid-flight timeout, a lost
response, a 5xx after the panel already applied the change) the provisioning
code issues a compensating delete. If that delete *also* fails there used to be
no durable trace: the enclosing transaction rolled back, so ``xui_clients`` knew
nothing about the identity we had just sent to the panel, and the user's retry
minted a fresh one — leaving the first client alive on the panel at full quota
forever.

``services/provisioning/manager.py::_record_possible_orphan`` now writes an
``OrphanedPanelClient`` row from an autonomous transaction at exactly that
moment, and this job drains it: for every unresolved row it re-issues the delete
until the panel confirms the client is gone.

Safety:
  * Every ``remote_client_id`` here is one WE minted for a single attempt — a
    fresh ``uuid4`` for X-UI, an 8-hex-suffixed username for the Marzban
    family — and identities are never reused. Deleting one therefore can never
    touch another user's live client.
  * A client that was never actually created is the happy case, not a problem:
    the panel says "not found" and we mark the row ``already_gone``.
  * Rows are marked resolved, never deleted, so the orphan history stays
    auditable.
  * After ``MAX_DELETE_ATTEMPTS`` a row drops out of the query and is escalated
    once via the log instead of hammering an unreachable panel indefinitely.
"""
from __future__ import annotations

import logging

from sqlalchemy import Select, select

from core.database import AsyncSessionFactory, utcnow
from models.xui import OrphanedPanelClient, XUIServerRecord
from services.panels.marzban import marzban_client_for_server, record_is_marzban_family
from services.xui.runtime import create_xui_client_for_server

logger = logging.getLogger(__name__)

# Give up (and escalate to the log) after this many failed deletes so a panel
# that is down for good doesn't get hammered on every run.
MAX_DELETE_ATTEMPTS = 8
# Bounded work per run, mirroring the reconciliation job — orphans are rare, and
# each row costs a panel round-trip.
BATCH_SIZE = 25

# The panel wording for "there is no such client". Sanaei's delClient answers
# `success: false` with a free-text msg rather than a 404, so a substring probe
# is the only signal available; a wrong guess merely costs a few more retries.
_ALREADY_GONE_MARKERS = (
    "not found",
    "not exist",
    "no such",
    "cannot find",
    "can not find",
)


def build_pending_orphan_select(*, limit: int = BATCH_SIZE) -> Select:
    """IDs of orphans still worth a delete attempt (extracted for testing)."""
    return (
        select(OrphanedPanelClient.id)
        .where(
            OrphanedPanelClient.resolved_at.is_(None),
            OrphanedPanelClient.attempts < MAX_DELETE_ATTEMPTS,
        )
        .order_by(OrphanedPanelClient.created_at.asc())
        .limit(limit)
    )


def is_already_gone(exc: BaseException) -> bool:
    """True when the failure proves the client is not on the panel any more."""
    if getattr(exc, "status_code", None) == 404:
        return True
    low = str(exc).lower()
    return any(marker in low for marker in _ALREADY_GONE_MARKERS)


async def _delete_remote_client(row: OrphanedPanelClient, server: XUIServerRecord) -> None:
    """Issue the panel-side delete for one orphan row."""
    if record_is_marzban_family(row):
        # delete_user already treats 404 as already-deleted.
        async with marzban_client_for_server(server) as client:
            await client.delete_user(row.remote_client_id)
        return
    if row.inbound_remote_id is None:
        raise ValueError("X-UI orphan row has no inbound id — cannot address the client")
    async with create_xui_client_for_server(server) as xui_client:
        await xui_client.delete_client(
            inbound_id=row.inbound_remote_id,
            client_id=row.remote_client_id,
        )


async def reconcile_orphaned_panel_clients() -> int:
    """Retry pending orphan deletes. Returns the number of rows resolved."""
    async with AsyncSessionFactory() as read_session:
        pending_ids = list(
            (await read_session.execute(build_pending_orphan_select())).scalars().all()
        )

    if not pending_ids:
        return 0

    resolved = 0
    for orphan_id in pending_ids:
        # One session per row: a failure on one orphan must not poison the
        # others with PendingRollbackError, and no lock is ever held across the
        # panel round-trip below.
        async with AsyncSessionFactory() as session:
            try:
                row = await session.scalar(
                    select(OrphanedPanelClient)
                    .where(OrphanedPanelClient.id == orphan_id)
                    .with_for_update(skip_locked=True)
                )
                if row is None or row.resolved_at is not None:
                    continue

                server = await session.get(XUIServerRecord, row.server_id)
                if server is None:
                    # The FK is ON DELETE CASCADE, so this only races an admin
                    # deleting the server. Without a base_url and credentials
                    # the client is unreachable; nothing left to do.
                    logger.warning(
                        "[ORPHAN-RECONCILE] server %s for orphan %s is gone — skipping",
                        row.server_id, orphan_id,
                    )
                    continue

                row.attempts += 1
                row.last_attempt_at = utcnow()
                try:
                    await _delete_remote_client(row, server)
                    row.resolved_at = utcnow()
                    row.resolution = "deleted"
                    row.last_error = None
                    resolved += 1
                    logger.info(
                        "[ORPHAN-RECONCILE] deleted orphaned %s client %s on %s (origin=%s)",
                        row.panel_kind, row.remote_client_id, server.name, row.origin,
                    )
                except Exception as exc:  # noqa: BLE001
                    if is_already_gone(exc):
                        row.resolved_at = utcnow()
                        row.resolution = "already_gone"
                        row.last_error = None
                        resolved += 1
                        logger.info(
                            "[ORPHAN-RECONCILE] orphan %s was never on %s — closing",
                            row.remote_client_id, server.name,
                        )
                    else:
                        row.last_error = str(exc)[:2000]
                        if row.attempts >= MAX_DELETE_ATTEMPTS:
                            logger.error(
                                "[ORPHAN-RECONCILE] giving up on %s client %s (inbound %s) on %s "
                                "after %d attempts — delete it manually: %s",
                                row.panel_kind, row.remote_client_id, row.inbound_remote_id,
                                server.name, row.attempts, exc,
                            )
                        else:
                            logger.warning(
                                "[ORPHAN-RECONCILE] attempt %d for client %s on %s failed: %s",
                                row.attempts, row.remote_client_id, server.name, exc,
                            )
                await session.commit()
            except Exception:  # noqa: BLE001
                # Never let one bad row abort the sweep.
                await session.rollback()
                logger.exception("[ORPHAN-RECONCILE] unexpected failure on orphan %s", orphan_id)

    return resolved
