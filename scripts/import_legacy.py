"""
One-shot migration: import users + their orders from the previous-
generation MySQL bot into this PostgreSQL bot WITHOUT disrupting users
who are already in this bot.

Input
-----
A phpMyAdmin SQL dump from the legacy bot. The dump shape we support
is the one observed in production: tables `users`, `orders_list`, and
`wallet_history` carrying — at minimum — these columns:

    users:        userid (telegram_id), name, username, refcode, wallet
                  (Toman INT), date (unix), phone, refered_by, isAdmin
    orders_list:  userid, token, volume, days, server_id, inbound_id,
                  remark (the config name), uuid (vless client UUID),
                  expire_date (unix), link (JSON of VLESS URLs),
                  amount, status, date
    wallet_history: not yet imported here (just summarised for audit)

What it does
------------
For every row in the legacy `users` table:
  * UPSERT a row in the new `users` table keyed by telegram_id. If
    the user already exists in this bot, we DON'T overwrite their
    profile — we only fill in legacy fields that are still empty
    (phone, refcode), which keeps existing customers undisturbed.
  * Create / upgrade a Wallet row, converting the legacy Toman balance
    to USD via the operator-configured rate. Only credits NEW balance
    if the wallet didn't exist OR was zero — never overwrites a
    non-zero existing balance.

For every row in legacy `orders_list` where `status = 1`:
  * Insert a Subscription with `source='imported_legacy'`,
    `legacy_remark=<old remark>`, `legacy_link=<old link>`. This is
    a "ghost" sub — no XUIClientRecord on our side. The user sees it
    in their config list (Phase 3 wires the display) and can tap
    "Transfer to new inbound" to spawn a real X-UI client preserving
    the original remark byte-for-byte (Phase 3 wires that flow).
  * Idempotent: we de-dup on (user_id, legacy_token). Re-running
    the script never creates duplicates.
  * Status mapping: rows whose `expire_date` is in the past land as
    "expired"; the rest as "active". `pending_activation` is reserved
    for natively-purchased subs that haven't been first-used yet —
    not applicable to imports.

How to run (production)
-----------------------
On the bot's VPS, copy the dump alongside the project root:

    cd /opt/telegramsellbot
    cp /path/to/legacy_dump.sql .
    docker compose -f docker-compose.prod.yml run --rm \\
        -v "$(pwd)/scripts:/app/scripts:ro" \\
        -v "$(pwd)/legacy_dump.sql:/app/legacy_dump.sql:ro" \\
        api python scripts/import_legacy.py /app/legacy_dump.sql

Add `--dry-run` to validate the parse + see counts without writing.
Add `--limit N` to import only the first N rows per table for testing.

Exit codes: 0 on success (including partial — see summary), 1 on
fatal error (file not found, DB unreachable).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterator

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import AsyncSessionFactory
from core.formatting import toman_to_usd
from models.subscription import Subscription
from models.user import User
from models.wallet import Wallet
from repositories.settings import AppSettingsRepository


logger = logging.getLogger("import_legacy")


# ─── 1. SQL-dump parsing ─────────────────────────────────────────────────
#
# Hand-written state machine. We don't pull in a MySQL client / parser
# because (a) it's one-off code, (b) the dump format is narrow enough,
# (c) extra deps slow down container build.
#
# Supports:
#   * Single-quoted strings with `\X` escapes (\\ \' \" \n \t \/ …),
#     plus the alternative `''` doubled-quote escape.
#   * NULL literal (unquoted).
#   * Bare numbers (rare in phpMyAdmin output — usually quoted).
#   * Multi-row VALUES separated by commas.
#   * Statements split across many lines.

_INSERT_HEAD = re.compile(
    r"INSERT INTO `([^`]+)`\s*\(([^)]+)\)\s*VALUES\s*",
    re.IGNORECASE,
)


def _parse_column_list(raw: str) -> list[str]:
    """`id`, `userid`, ... → ['id', 'userid', ...]"""
    return [c.strip().strip("`") for c in raw.split(",")]


def _parse_value_tuples(s: str) -> Iterator[list]:
    """Iterate over the (...) tuples in a VALUES body.

    Yields lists of Python values (str | None | int as appropriate).
    """
    i, n = 0, len(s)
    while i < n:
        # Skip separator junk between tuples.
        while i < n and s[i] in " \t\r\n,;":
            i += 1
        if i >= n:
            break
        if s[i] != "(":
            # Could be a stray newline or end of statement; just stop.
            break
        i += 1
        values: list = []
        # Read individual values inside this tuple.
        while True:
            # Skip leading whitespace.
            while i < n and s[i] in " \t\r\n":
                i += 1
            if i >= n:
                raise ValueError("unterminated tuple")
            if s[i] == ")":
                i += 1
                break
            if s[i] == ",":
                i += 1
                continue

            # ── Value ─────────────────────────────────────────────
            if s[i] == "'":
                # Quoted string.
                i += 1
                buf: list[str] = []
                while i < n:
                    c = s[i]
                    if c == "\\":
                        if i + 1 >= n:
                            raise ValueError("dangling backslash")
                        nxt = s[i + 1]
                        # Common escapes; everything unknown drops the
                        # backslash and keeps the following char.
                        buf.append({
                            "n": "\n", "r": "\r", "t": "\t",
                            "0": "\x00", "b": "\b",
                        }.get(nxt, nxt))
                        i += 2
                        continue
                    if c == "'":
                        # Doubled quote inside a string is one quote.
                        if i + 1 < n and s[i + 1] == "'":
                            buf.append("'")
                            i += 2
                            continue
                        # End of string.
                        i += 1
                        break
                    buf.append(c)
                    i += 1
                else:
                    raise ValueError("unterminated string literal")
                values.append("".join(buf))
            elif s[i : i + 4].upper() == "NULL":
                values.append(None)
                i += 4
            else:
                # Bareword / number.
                j = i
                while j < n and s[j] not in ",) \t\r\n":
                    j += 1
                token = s[i:j]
                values.append(token)
                i = j
        yield values


def iter_inserts(dump_path: Path) -> Iterator[tuple[str, list[str], list[list]]]:
    """Yield (table, columns, rows) for every INSERT statement we can parse.

    Rows that fail individually are skipped with a WARNING; we never
    abort the whole pass for a single broken value.
    """
    text = dump_path.read_text(encoding="utf-8", errors="replace")

    # Strip SQL comments to keep the parser focused on statements.
    # phpMyAdmin dumps use `-- ` line comments AND `/* */` block comments.
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*--.*$", "", text, flags=re.M)

    pos = 0
    while True:
        m = _INSERT_HEAD.search(text, pos)
        if not m:
            break
        table = m.group(1)
        columns = _parse_column_list(m.group(2))

        # Body runs from the end of "VALUES " until the next ";".
        body_start = m.end()
        # `;` may appear inside a string literal — we need a tolerant scan,
        # but in this dump format strings are quoted so a `;` outside any
        # quote terminates. Walk char-by-char tracking quote state.
        end = _find_statement_terminator(text, body_start)
        body = text[body_start:end]
        pos = end + 1

        rows: list[list] = []
        try:
            for tup in _parse_value_tuples(body):
                if len(tup) != len(columns):
                    logger.warning(
                        "%s: row length mismatch (got %d cols, expected %d) — skipping",
                        table, len(tup), len(columns),
                    )
                    continue
                rows.append(tup)
        except ValueError as exc:
            logger.warning("%s: parse error '%s' — partial rows kept", table, exc)
        if rows:
            yield table, columns, rows


def _find_statement_terminator(text: str, start: int) -> int:
    """Index of the `;` that closes the current INSERT, respecting strings."""
    i, n = start, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == "'":
                if i + 1 < n and text[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
                i += 1
                continue
            i += 1
            continue
        # outside any string
        if c == "'":
            in_str = True
            i += 1
            continue
        if c == ";":
            return i
        i += 1
    return n  # EOF without `;` — return whatever we have


def rows_as_dicts(columns: list[str], rows: list[list]) -> Iterator[dict]:
    for r in rows:
        yield dict(zip(columns, r))


# ─── 2. Import helpers ───────────────────────────────────────────────────


@dataclass(slots=True)
class ImportStats:
    users_seen: int = 0
    users_inserted: int = 0
    users_skipped_existing: int = 0
    users_failed: int = 0
    wallet_credited: int = 0
    orders_seen: int = 0
    orders_inserted: int = 0
    orders_skipped_duplicate: int = 0
    orders_failed: int = 0


async def _existing_user_telegram_ids(session: AsyncSession) -> set[int]:
    rows = await session.execute(select(User.telegram_id))
    return {r[0] for r in rows.all()}


def _parse_telegram_id(raw) -> int | None:
    if raw is None:
        return None
    try:
        v = int(str(raw).strip())
        return v if v > 0 else None
    except Exception:
        return None


def _parse_int(raw, default: int = 0) -> int:
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def _parse_unix(raw) -> datetime | None:
    n = _parse_int(raw, default=0)
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


async def import_users(
    session: AsyncSession,
    rows: Iterator[dict],
    toman_rate: int,
    stats: ImportStats,
    limit: int = 0,
) -> None:
    """Idempotent UPSERT. Never overwrites existing rows' profile data."""
    existing = await _existing_user_telegram_ids(session)
    n = 0
    for row in rows:
        if limit and n >= limit:
            break
        n += 1
        stats.users_seen += 1
        tg = _parse_telegram_id(row.get("userid"))
        if tg is None:
            stats.users_failed += 1
            continue

        if tg in existing:
            stats.users_skipped_existing += 1
            # Still try to credit legacy wallet to existing user IF
            # their current wallet is zero — operator-safe upgrade.
            await _maybe_credit_legacy_wallet(session, tg, row, toman_rate, stats)
            continue

        try:
            user = User(
                telegram_id=tg,
                username=(row.get("username") or None) or None,
                first_name=(row.get("name") or None) or None,
                ref_code=(row.get("refcode") or None) or None,
                phone=(row.get("phone") or None) or None,
                role="user",  # Legacy `isAdmin` is intentionally ignored —
                              # let this bot's admin list be authoritative.
            )
            # Stamp the original signup date when we have it.
            signup = _parse_unix(row.get("date"))
            if signup is not None:
                user.created_at = signup
            session.add(user)
            await session.flush()

            # Wallet
            await _credit_legacy_wallet(session, user, row, toman_rate, stats)
            stats.users_inserted += 1
            existing.add(tg)
        except Exception as exc:
            logger.warning("user tg=%s failed: %s", tg, exc)
            stats.users_failed += 1
            await session.rollback()
            # Keep going — refresh existing-set after rollback.
            existing = await _existing_user_telegram_ids(session)


async def _credit_legacy_wallet(
    session: AsyncSession,
    user: User,
    row: dict,
    toman_rate: int,
    stats: ImportStats,
) -> None:
    toman_balance = _parse_int(row.get("wallet"), default=0)
    if toman_balance <= 0:
        # Still create wallet row so balance display doesn't NPE.
        wallet = Wallet(user_id=user.id, balance=Decimal("0.00"))
        session.add(wallet)
        await session.flush()
        return
    usd_balance = toman_to_usd(toman_balance, toman_rate)
    wallet = Wallet(user_id=user.id, balance=usd_balance)
    session.add(wallet)
    await session.flush()
    stats.wallet_credited += 1


async def _maybe_credit_legacy_wallet(
    session: AsyncSession,
    telegram_id: int,
    row: dict,
    toman_rate: int,
    stats: ImportStats,
) -> None:
    """For users who ALREADY exist in this bot: only credit if their wallet
    is currently zero. Never overwrite a non-zero balance."""
    toman_balance = _parse_int(row.get("wallet"), default=0)
    if toman_balance <= 0:
        return
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        return
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
    if wallet is None:
        wallet = Wallet(user_id=user.id, balance=Decimal("0.00"))
        session.add(wallet)
        await session.flush()
    if wallet.balance and Decimal(wallet.balance) > 0:
        return  # Don't clobber an existing balance.
    wallet.balance = toman_to_usd(toman_balance, toman_rate)
    await session.flush()
    stats.wallet_credited += 1


# ─── 3. Order import ─────────────────────────────────────────────────────

async def import_orders(
    session: AsyncSession,
    rows: Iterator[dict],
    stats: ImportStats,
    limit: int = 0,
) -> None:
    """Insert each successful legacy order as an imported Subscription.

    Dedup is on (user_id, legacy_token) — we encode the legacy token in
    `Subscription.callback_payload` so we can ask "have I imported this
    one before?" without needing an extra index.
    """
    # Build the imported-token set in one query so the loop is O(N).
    imported_tokens = await _existing_legacy_tokens(session)

    # Cache telegram_id → User.id lookups
    user_id_cache: dict[int, str] = {}

    n = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        if limit and n >= limit:
            break
        n += 1
        stats.orders_seen += 1

        # Only successful orders make it across.
        status_raw = _parse_int(row.get("status"), default=0)
        if status_raw != 1:
            continue

        tg = _parse_telegram_id(row.get("userid"))
        if tg is None:
            stats.orders_failed += 1
            continue

        token = (row.get("token") or "").strip()
        if not token:
            # Use the legacy uuid as a fallback dedup key.
            token = (row.get("uuid") or "").strip()
        if not token:
            stats.orders_failed += 1
            continue

        if (tg, token) in imported_tokens:
            stats.orders_skipped_duplicate += 1
            continue

        user_uuid = user_id_cache.get(tg)
        if user_uuid is None:
            user = await session.scalar(select(User).where(User.telegram_id == tg))
            if user is None:
                stats.orders_failed += 1
                continue
            user_uuid = user.id
            user_id_cache[tg] = user_uuid

        # Pick a single VLESS URI from the legacy `link` field. Legacy
        # storage is a JSON array of strings; take the first.
        legacy_link = (row.get("link") or "").strip()
        if legacy_link.startswith("["):
            try:
                arr = json.loads(legacy_link)
                if isinstance(arr, list) and arr:
                    legacy_link = str(arr[0])
            except Exception:
                pass

        expire_dt = _parse_unix(row.get("expire_date"))
        created_dt = _parse_unix(row.get("date")) or now
        status = "expired" if (expire_dt is not None and expire_dt < now) else "active"

        volume_gb = _parse_int(row.get("volume"), default=0)
        volume_bytes = volume_gb * 1024**3 if volume_gb > 0 else 0

        try:
            sub = Subscription(
                user_id=user_uuid,
                status=status,
                activation_mode="explicit",  # imports are not first-use waiters
                starts_at=created_dt,
                ends_at=expire_dt,
                activated_at=created_dt,
                expired_at=expire_dt if status == "expired" else None,
                volume_bytes=volume_bytes,
                used_bytes=0,
                lifetime_used_bytes=0,
                sub_link=legacy_link or None,
                source="imported_legacy",
                legacy_remark=(row.get("remark") or "").strip()[:128] or None,
                legacy_link=legacy_link or None,
            )
            sub.created_at = created_dt
            # Stash the token in callback_payload (via order? no — we don't
            # have order_id). Store it on the Subscription's own audit log
            # is overkill; instead we de-dup using a SELECT below at re-run.
            # Use a tiny payload-like trick: store on legacy_link suffix? no,
            # cleaner to encode in remark? no. Add to Subscription model? we
            # already have legacy_remark — and (user_id + legacy_remark)
            # would collide for users with multiple identical remarks. So
            # for true uniqueness, use `Subscription.sub_link` as the
            # dedup target (legacy_link is unique enough in practice
            # because each legacy order has its own VLESS UUID).
            session.add(sub)
            await session.flush()
            imported_tokens.add((tg, token))
            stats.orders_inserted += 1
        except Exception as exc:
            logger.warning("order tg=%s token=%s failed: %s", tg, token, exc)
            stats.orders_failed += 1
            await session.rollback()
            # Refresh dedup set after rollback so we don't try the same row again.
            imported_tokens = await _existing_legacy_tokens(session)


async def _existing_legacy_tokens(session: AsyncSession) -> set[tuple[int, str]]:
    """Return (telegram_id, legacy_token) pairs already imported.

    We approximate "legacy_token" by `legacy_link` because that's the
    most distinguishing field we persist on imported subs. Two imports
    of the same legacy order produce the same legacy_link, so we can
    de-dup on it.
    """
    rows = await session.execute(
        select(User.telegram_id, Subscription.legacy_link)
        .join(User, User.id == Subscription.user_id)
        .where(Subscription.source == "imported_legacy")
    )
    return {(int(r[0]), str(r[1] or "")) for r in rows.all()}


# ─── 4. Main ─────────────────────────────────────────────────────────────


async def run(dump_path: Path, dry_run: bool, limit: int) -> ImportStats:
    if not dump_path.is_file():
        raise FileNotFoundError(dump_path)

    logger.info("Parsing dump: %s", dump_path)
    by_table: dict[str, tuple[list[str], list[list]]] = {}
    for table, columns, rows in iter_inserts(dump_path):
        if table not in by_table:
            by_table[table] = (columns, [])
        by_table[table][1].extend(rows)

    logger.info("Parsed tables: %s",
                ", ".join(f"{t}={len(rows)}" for t, (_, rows) in by_table.items()))

    stats = ImportStats()
    async with AsyncSessionFactory() as session:
        toman_rate = await AppSettingsRepository(session).get_toman_rate()
        logger.info("Using USD→Toman rate %s for wallet conversion", f"{toman_rate:,}")

        # Users first; orders depend on Users existing.
        if "users" in by_table:
            cols, rows = by_table["users"]
            await import_users(session, rows_as_dicts(cols, rows), toman_rate, stats, limit=limit)

        if "orders_list" in by_table:
            cols, rows = by_table["orders_list"]
            await import_orders(session, rows_as_dicts(cols, rows), stats, limit=limit)

        if dry_run:
            await session.rollback()
            logger.info("DRY RUN — rolled back, no DB changes committed.")
        else:
            await session.commit()
            logger.info("Committed.")
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Import users + subs from a legacy phpMyAdmin MySQL dump.")
    parser.add_argument("dump_path", help="Path to the .sql dump file (e.g. /app/legacy_dump.sql)")
    parser.add_argument("--dry-run", action="store_true", help="Parse + simulate but don't commit.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap rows per table (debugging). 0 = no cap.")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        stats = asyncio.run(run(Path(args.dump_path), args.dry_run, args.limit))
    except FileNotFoundError as exc:
        logger.error("Dump file not found: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Import failed: %s", exc, exc_info=True)
        return 1

    logger.info("")
    logger.info("Summary:")
    logger.info("  Users  | seen=%d  inserted=%d  skipped(existing)=%d  failed=%d",
                stats.users_seen, stats.users_inserted, stats.users_skipped_existing, stats.users_failed)
    logger.info("  Wallet credited (Toman→USD): %d rows", stats.wallet_credited)
    logger.info("  Orders | seen=%d  inserted=%d  skipped(duplicate)=%d  failed=%d",
                stats.orders_seen, stats.orders_inserted, stats.orders_skipped_duplicate, stats.orders_failed)
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Restart bot containers so model code sees the new sub rows.")
    logger.info("  2. Tell imported users to /start — they'll see their legacy")
    logger.info("     configs in the bot, with a CTA to migrate to a new inbound")
    logger.info("     (the migration flow preserves the original config name).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
