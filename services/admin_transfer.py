"""
Admin-initiated config transfer.

Lets an admin move a customer's config(s) — a single subscription or ALL
of the customer's subscriptions — to a DIFFERENT account. This is the
single source of truth shared by every surface (bot admin panel, mini-app
admin, web dashboard) so the rules stay identical everywhere.

Design choice (operator-selected): the transfer ONLY changes DB ownership
(`Subscription.user_id`). It does NOT rotate the X-UI client UUID/subId, so
the existing subscription link keeps working for whoever already holds it.
Contrast with the user-facing transfer (apps/bot/handlers/user/transfer.py)
which rotates the panel identity to kill the sender's old links.

Safety properties:
  * Only subscriptions that actually belong to the source user are moved
    (no cross-user reassignment by guessing IDs).
  * The affected subscription rows are locked FOR UPDATE for the duration.
  * Source == target is rejected.
  * Every moved subscription gets its own AuditLog row.
"""
from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.audit import AuditLog
from models.plan import Plan
from models.subscription import Subscription
from models.user import User
from models.xui import XUIClientRecord


logger = logging.getLogger(__name__)


_STATUS_FA = {
    "active": "فعال",
    "pending_activation": "در انتظار فعال‌سازی",
    "expired": "منقضی",
    "disabled": "غیرفعال",
    "refunded": "مرجوع‌شده",
    "cancelled": "لغوشده",
}


def status_fa(status: str | None) -> str:
    return _STATUS_FA.get(status or "", status or "—")


def config_label(sub: Subscription) -> str:
    """A short, human-recognisable label for a subscription, safe to call
    only when xui_client + plan were eager-loaded (use list_transferable_configs)."""
    name = None
    xc = getattr(sub, "xui_client", None)
    if xc is not None:
        name = getattr(xc, "username", None) or getattr(xc, "email", None)
    if not name:
        plan = getattr(sub, "plan", None)
        if plan is not None:
            name = getattr(plan, "name", None)
    if not name:
        name = f"config-{str(sub.id)[:8]}"
    return f"{name} · {status_fa(sub.status)}"


class AdminTransferError(Exception):
    """Raised when an admin config-transfer cannot proceed (validation)."""


async def resolve_target_user(session: AsyncSession, query: str) -> User | None:
    """Look up a transfer TARGET by numeric telegram_id or @username.

    Accepts a free-text identifier (the way the bot / mini-app collect it).
    Returns the User or None if not found.
    """
    q = (query or "").strip().lstrip("@")
    if not q:
        return None
    if q.isdigit():
        try:
            by_id = await session.scalar(select(User).where(User.telegram_id == int(q)))
        except (ValueError, OverflowError):
            by_id = None
        if by_id is not None:
            return by_id
    # Username match is case-insensitive (Telegram usernames are unique
    # case-insensitively).
    return await session.scalar(
        select(User).where(func.lower(User.username) == q.lower())
    )


async def list_transferable_configs(
    session: AsyncSession, source_user_id: UUID
) -> list[Subscription]:
    """All of a user's subscriptions (most recent first), eager-light — used
    by the surfaces to render a pick-list. No status filter: an admin moving
    an account wants to see everything they could move."""
    result = await session.execute(
        select(Subscription)
        .options(
            selectinload(Subscription.xui_client),
            selectinload(Subscription.plan),
        )
        .where(Subscription.user_id == source_user_id)
        .order_by(Subscription.created_at.desc())
    )
    return list(result.scalars().all())


def _looks_like_telegram_id(query: str) -> int | None:
    """If `query` is a bare numeric Telegram id (optionally @-prefixed), return
    it as an int; otherwise None. Kept pure so it can be unit-tested without a DB."""
    s = (query or "").strip().lstrip("@")
    if s.isdigit() and 3 <= len(s) <= 15:
        try:
            return int(s)
        except (ValueError, OverflowError):
            return None
    return None


def owner_label(user: User | None) -> str:
    """Short, human label for a config's owner (for the admin search list)."""
    if user is None:
        return "بدون مالک"
    if user.username:
        return f"@{user.username}"
    name = (user.first_name or "").strip()
    if name:
        return f"{name} ({user.telegram_id})"
    return str(user.telegram_id)


def config_search_label(sub: Subscription) -> str:
    """Label for the GLOBAL search list: `config name · status · owner`.

    Safe only when xui_client + plan + user were eager-loaded (search_configs
    does this)."""
    return f"{config_label(sub)} · 👤 {owner_label(getattr(sub, 'user', None))}"


def _config_search_filter(query: str):
    """Build the WHERE expression for a global config search.

    Returns a SQLAlchemy boolean expression, or None to match EVERYTHING
    (empty query, or the explicit "show all" tokens). Pure/synchronous so the
    matching rules are easy to reason about and test.
    """
    q = (query or "").strip()
    if not q or q in {"*", "همه", "all", "*همه*"}:
        return None
    like = f"%{q}%"
    conditions = [
        XUIClientRecord.username.ilike(like),
        XUIClientRecord.email.ilike(like),
        XUIClientRecord.client_uuid.ilike(like),
        XUIClientRecord.sub_link.ilike(like),
        Subscription.legacy_remark.ilike(like),
        Subscription.user_note.ilike(like),
        Subscription.sub_link.ilike(like),
        Plan.name.ilike(like),
        User.username.ilike(like),
        User.first_name.ilike(like),
    ]
    tid = _looks_like_telegram_id(q)
    if tid is not None:
        conditions.append(User.telegram_id == tid)
    return or_(*conditions)


async def search_configs(
    session: AsyncSession,
    query: str | None,
    *,
    limit: int = 8,
    offset: int = 0,
) -> tuple[list[Subscription], int]:
    """Search across EVERY config in the system (all owners) — admin tool.

    Matches `query` (case-insensitive substring) against the config's panel
    identity (username / email / uuid / sub-link), the subscription's legacy
    remark and user note, the plan name, and the owner (username / first name).
    A bare numeric query ALSO matches the owner's telegram_id exactly. An empty
    query (or "*" / "همه" / "all") lists everything, most-recent first.

    Returns (page_rows, total_count). Rows are eager-loaded with xui_client,
    plan and user so config_search_label() / detail rendering are safe.

    All joins are one-to-one (xui_client) or many-to-one (plan, user), so there
    is no row fan-out — limit/offset paginate accurately.
    """
    where = _config_search_filter(query or "")

    def _with_joins(stmt):
        stmt = (
            stmt.join(User, User.id == Subscription.user_id)
            .outerjoin(XUIClientRecord, XUIClientRecord.subscription_id == Subscription.id)
            .outerjoin(Plan, Plan.id == Subscription.plan_id)
        )
        if where is not None:
            stmt = stmt.where(where)
        return stmt

    count_stmt = _with_joins(
        select(func.count(func.distinct(Subscription.id))).select_from(Subscription)
    )
    total = int(await session.scalar(count_stmt) or 0)
    if total == 0:
        return [], 0

    rows_stmt = (
        _with_joins(select(Subscription))
        .options(
            selectinload(Subscription.xui_client),
            selectinload(Subscription.plan),
            selectinload(Subscription.user),
        )
        .order_by(Subscription.created_at.desc())
        .limit(max(1, limit))
        .offset(max(0, offset))
    )
    result = await session.execute(rows_stmt)
    rows = list(result.scalars().unique().all())
    return rows, total


async def admin_transfer_configs(
    session: AsyncSession,
    *,
    source_user_id: UUID,
    target_user_id: UUID,
    subscription_ids: list[UUID] | None = None,
    actor_label: str,
    actor_user_id: UUID | None = None,
) -> dict:
    """Reassign ownership of the source user's config(s) to the target user.

    Args:
        source_user_id: current owner.
        target_user_id: new owner.
        subscription_ids: specific subs to move; None => move ALL of source's.
        actor_label: free-text actor descriptor for the audit trail, e.g.
            "bot_admin:<tg_id>", "miniapp_admin:<tg_id>", "dashboard_admin:<id>".
        actor_user_id: the acting admin's bot User.id when available (None for
            dashboard admins, who live in a separate table).

    Returns a summary dict: {count, transferred[], target_user_id,
    target_telegram_id, target_name, source_telegram_id}.

    Raises AdminTransferError on any validation problem. The caller owns the
    commit.
    """
    if source_user_id == target_user_id:
        raise AdminTransferError("کاربر مبدأ و مقصد نمی‌توانند یکی باشند.")

    source = await session.get(User, source_user_id)
    if source is None:
        raise AdminTransferError("کاربر مبدأ پیدا نشد.")
    target = await session.get(User, target_user_id)
    if target is None:
        raise AdminTransferError("کاربر مقصد پیدا نشد.")

    # Load exactly the subs we're allowed to move — always scoped to the
    # source owner so a forged/foreign subscription_id can never be moved.
    stmt = (
        select(Subscription)
        .where(Subscription.user_id == source_user_id)
        .with_for_update()
    )
    requested: set[UUID] | None = None
    if subscription_ids is not None:
        requested = set(subscription_ids)
        if not requested:
            raise AdminTransferError("هیچ کانفیگی برای انتقال انتخاب نشده است.")
        stmt = stmt.where(Subscription.id.in_(requested))

    subs = list((await session.execute(stmt)).scalars().all())

    if requested is not None:
        found = {s.id for s in subs}
        missing = requested - found
        if missing:
            raise AdminTransferError(
                "برخی از کانفیگ‌های انتخاب‌شده متعلق به این کاربر نیستند."
            )

    if not subs:
        raise AdminTransferError("این کاربر هیچ کانفیگی برای انتقال ندارد.")

    transferred: list[str] = []
    for sub in subs:
        sub.user_id = target_user_id
        transferred.append(str(sub.id))
        session.add(
            AuditLog(
                actor_user_id=actor_user_id,
                action="admin_transfer_config",
                entity_type="subscription",
                entity_id=sub.id,
                payload={
                    "from_user_id": str(source_user_id),
                    "from_telegram_id": source.telegram_id,
                    "to_user_id": str(target_user_id),
                    "to_telegram_id": target.telegram_id,
                    "actor": actor_label,
                    "rotated": False,
                },
            )
        )

    await session.flush()
    logger.info(
        "admin_transfer: moved %d sub(s) from user %s to user %s (by %s): %s",
        len(transferred), source_user_id, target_user_id, actor_label, transferred,
    )
    return {
        "count": len(transferred),
        "transferred": transferred,
        "target_user_id": str(target_user_id),
        "target_telegram_id": target.telegram_id,
        "target_name": target.first_name or target.username or str(target.telegram_id),
        "source_telegram_id": source.telegram_id,
    }
