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
from dataclasses import dataclass, field
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
from models.xui import XUIClientRecord
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
    # Already-imported subs that had volume=0 or ends_at=null on an
    # earlier run and got patched in place this run. Letting the
    # operator see "X fixed" makes it obvious that re-runs are recovering
    # broken rows rather than no-oping.
    orders_updated: int = 0
    # ── Diagnostics surfaced to the bot summary ──────────────────────
    # The orders_list column names + a sample status=1 row, so when
    # volume still resolves to 0 the operator's screenshot of the bot
    # message tells us the exact schema (no log-grepping needed).
    orders_columns: list[str] = field(default_factory=list)
    orders_sample_row: dict = field(default_factory=dict)
    # Name of the column volume was actually read from (None = not found).
    volume_source_column: str | None = None
    # How many orders ended up with a non-zero parsed volume.
    orders_with_volume: int = 0
    # How many subs had their volume/expiry recovered from the legacy
    # panel's subscription-userinfo header (post-import http pass).
    volume_recovered_from_panel: int = 0
    sublink_fetch_attempts: int = 0
    # Duplicate imported subs removed by the dedupe pass.
    orders_deduped: int = 0
    # ── Cumulative DB state after this run (the honest picture) ──────
    imported_total: int = 0            # all imported_legacy subs in DB
    imported_with_volume: int = 0      # …that now have volume_bytes > 0
    imported_active: int = 0           # …that are active / pending
    imported_active_no_volume: int = 0 # active but still volume 0 (needs attention)


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
    s = str(raw).strip()
    if not s:
        return default
    # Accept floats ("30.0"), scientific notation ("3e10"), and numbers with
    # stray non-digit tail ("30GB", "100 MB") — strip the tail conservatively.
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return int(float(s))
    except ValueError:
        pass
    # Pull leading numeric run.
    import re as _re
    m = _re.match(r"^[-+]?\d+(?:\.\d+)?", s)
    if m:
        try:
            return int(float(m.group(0)))
        except ValueError:
            return default
    return default


def _parse_unix(raw) -> datetime | None:
    n = _parse_int(raw, default=0)
    if n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


# ─── Smart volume + expiry detection ───────────────────────────────────
#
# Different forks of Mirzabot/Faoxima use different column names. We try
# a list of candidates in priority order; the first one with a non-zero
# value wins. Same idea for expiry — `expire_date` is most common but
# some forks only store `days` and let the client compute.

# NOTE: matching is CASE-INSENSITIVE (see _ci_get), so we only need one
# casing of each name here. The list is broad to cover the many Persian
# VPN bot forks (Mirza, Marzban-sell, custom orders_list schemas, …).
_VOLUME_COLUMNS = (
    "volume", "volume_bytes", "total_volume", "total_bytes",
    "data", "data_limit", "datalimit", "package_data", "totalgb", "total_gb",
    "volumetotal", "datasize", "package_volume",
    "traffic", "total_traffic", "limit_traffic", "traffic_limit",
    "flow", "quota", "size", "gb", "giga", "gig", "hajm", "vol",
    "limitvolume", "limit_volume", "volume_limit", "usage_limit",
)

_EXPIRE_COLUMNS = (
    "expire_date", "expiredate", "expire_at", "expires_at",
    "expiry", "expiry_time", "expiretime", "expirationdate",
    "exp", "exp_date", "exp_time", "enddate", "end_date", "end_time",
)

_DAYS_COLUMNS = (
    "days", "period", "duration", "duration_days", "package_days",
    "day", "expire_days", "service_days", "time", "muddat",
)

_REMARK_COLUMNS = (
    "remark", "name", "config_name", "username", "title", "subject",
)

_LINK_COLUMNS = (
    "link", "sub_link", "subscription_link", "config_link",
    "vless", "vless_uri", "url",
)

_UUID_COLUMNS = (
    "uuid", "client_id", "client_uuid", "id_client",
)


def _ci_get(row: dict, column: str):
    """Case-insensitive column lookup.

    SQL column names are case-insensitive in MySQL, but our row dict is
    keyed by the EXACT name from the dump. So a dump column named
    `Volume` / `VOLUME` would never match the lowercase candidate
    `"volume"`. Match case-insensitively (and ignore surrounding spaces)
    so column-name casing differences across bot forks don't silently
    drop the value. Returns (matched_key, value) or (None, None).
    """
    if column in row:
        return column, row[column]
    target = column.strip().lower()
    for k, v in row.items():
        if str(k).strip().lower() == target:
            return k, v
    return None, None


def _first_nonzero_int(row: dict, columns: tuple[str, ...]) -> tuple[str | None, int]:
    """Return (which_column_won, integer_value). 0 if none had a value."""
    for c in columns:
        matched_key, raw = _ci_get(row, c)
        if matched_key is None:
            continue
        v = _parse_int(raw, default=0)
        if v != 0:  # 0 means "missing/unlimited/zero" → keep trying other cols
            return matched_key, v
    return None, 0


def _first_nonempty(row: dict, columns: tuple[str, ...]) -> tuple[str | None, str]:
    for c in columns:
        matched_key, raw = _ci_get(row, c)
        if matched_key is None or raw is None:
            continue
        s = str(raw).strip()
        if s:
            return matched_key, s
    return None, ""


def _extract_links(raw: str) -> tuple[str | None, str | None]:
    """From the legacy `link` field, return (http_sub_link, vless_uri).

    The field is commonly a JSON array holding BOTH an http(s)
    subscription URL (e.g. http://check.subconnectur.store:2082/sub/<token>)
    AND a vless:// config URI. We want the http one because its
    `subscription-userinfo` response header carries the REAL volume +
    expiry from the legacy panel (the legacy bot's own DB stored
    volume=0). We keep the vless one for display / as a fallback.

    Handles: JSON array, plain http string, plain vless string,
    newline/comma-separated lists.
    """
    if not raw:
        return None, None
    s = raw.strip()
    candidates: list[str] = []
    if s.startswith("["):
        try:
            arr = json.loads(s)
            if isinstance(arr, list):
                candidates = [str(x) for x in arr if x]
        except Exception:
            candidates = [s]
    if not candidates:
        # split on whitespace/newline/comma in case it's a delimited list
        candidates = [p for p in re.split(r"[\s,]+", s) if p]
    http_link: str | None = None
    vless_link: str | None = None
    for c in candidates:
        cl = c.strip().strip('"').strip("'")
        if not cl:
            continue
        if cl.startswith(("http://", "https://")):
            if http_link is None:
                http_link = cl
        elif "://" in cl:  # vless:// vmess:// trojan:// ss:// …
            if vless_link is None:
                vless_link = cl
    return http_link, vless_link


def _normalize_volume_to_bytes(value: int) -> int:
    """Auto-detect units. Mirzabot forks store volume in either bytes
    (X-UI totalGB which is actually bytes) OR in GB integer.

    Heuristic by magnitude:
        value < 1024            → treat as GB    (e.g. 30 → 30 GB)
        1024 ≤ value < 10**9    → treat as MB    (e.g. 30720 → 30 GB)
        value ≥ 10**9           → treat as bytes (e.g. 32212254720 → 30 GB)
    """
    if value <= 0:
        return 0
    if value < 1024:
        return value * (1024 ** 3)
    if value < 10 ** 9:
        return value * (1024 ** 2)
    return value


def _resolve_expiry(row: dict, created_dt: datetime, now: datetime) -> datetime | None:
    """Find the expiry from the row. Tries expire_date columns first,
    then computes from days+date. Returns None if both attempts fail."""
    for c in _EXPIRE_COLUMNS:
        _, raw = _ci_get(row, c)
        v = _parse_unix(raw)
        if v is not None:
            return v
    days_col, days = _first_nonzero_int(row, _DAYS_COLUMNS)
    if days > 0:
        from datetime import timedelta as _td
        return (created_dt or now) + _td(days=days)
    return None


async def _existing_ref_codes(session: AsyncSession) -> set[str]:
    rows = await session.execute(select(User.ref_code).where(User.ref_code.is_not(None)))
    return {r[0] for r in rows.all() if r[0]}


async def import_users(
    session: AsyncSession,
    rows: Iterator[dict],
    toman_rate: int,
    stats: ImportStats,
    limit: int = 0,
) -> None:
    """Idempotent UPSERT. Never overwrites existing rows' profile data.

    Two important details for re-runs / partial recovery:

    * Each row runs inside its own ``session.begin_nested()`` SAVEPOINT.
      A failure on one row only rolls back THAT row — earlier inserts
      stay flushed. (The previous implementation called the bare
      ``session.rollback()`` which threw away every successful insert
      in the same transaction; that's why an earlier attempt landed
      with ``inserted=0`` despite 1100+ rows being touched.)

    * The legacy ``phone`` column is intentionally NOT copied — this
      bot's ``users`` table has no ``phone`` field, and a stray kwarg
      to ``User(...)`` raises ``TypeError`` on every row.

    * The legacy ``refcode`` is preserved when it doesn't collide with
      an existing ref_code on this bot (``ref_code`` is UNIQUE); on
      collision we leave it NULL so the row still imports.
    """
    existing = await _existing_user_telegram_ids(session)
    existing_refs = await _existing_ref_codes(session)
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
            try:
                async with session.begin_nested():
                    await _maybe_credit_legacy_wallet(session, tg, row, toman_rate, stats)
            except Exception as exc:
                logger.warning("wallet-credit existing tg=%s failed: %s", tg, exc)
            continue

        # Pick a ref_code that doesn't collide. Legacy `refcode` can be
        # the user's own personal code OR (in some legacy bots) the
        # referrer's code shared by many users — without ever importing
        # we can't tell. We try it first; on collision we drop it.
        legacy_refcode = (row.get("refcode") or "").strip() or None
        chosen_ref = legacy_refcode if (legacy_refcode and legacy_refcode not in existing_refs) else None

        try:
            async with session.begin_nested():
                user = User(
                    telegram_id=tg,
                    username=(row.get("username") or None) or None,
                    first_name=(row.get("name") or None) or None,
                    ref_code=chosen_ref,
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
            if chosen_ref:
                existing_refs.add(chosen_ref)
        except Exception as exc:
            logger.warning("user tg=%s failed: %s", tg, exc)
            stats.users_failed += 1


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

    Re-runs of this function are SAFE and DESIRABLE:
      * Dedup on (user_id, legacy_link) — same row is never inserted twice.
      * Already-imported rows whose volume_bytes or ends_at landed as
        garbage on an earlier run get UPDATED in place this time, if the
        new parse produced a non-zero value. That's how the operator
        fixes the "0 B / منقضی" bug without truncating the table.

    Volume / expiry parsing is now tolerant of column-name variation
    across Mirzabot/Faoxima forks. See _VOLUME_COLUMNS, _EXPIRE_COLUMNS,
    _DAYS_COLUMNS up top.
    """
    # Build the imported-token set in one query so the loop is O(N).
    imported_tokens = await _existing_legacy_tokens(session)

    # Cache telegram_id → User.id lookups
    user_id_cache: dict[int, str] = {}

    # Diagnostic: track which columns the script actually found.
    column_hits: dict[str, int] = {"volume": 0, "expire": 0, "days": 0}

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
            _, token = _first_nonempty(row, _UUID_COLUMNS)
        if not token:
            stats.orders_failed += 1
            continue

        user_uuid = user_id_cache.get(tg)
        if user_uuid is None:
            user = await session.scalar(select(User).where(User.telegram_id == tg))
            if user is None:
                stats.orders_failed += 1
                continue
            user_uuid = user.id
            user_id_cache[tg] = user_uuid

        # Extract BOTH the http subscription URL and the vless URI from the
        # legacy `link` field. The http one is gold: its
        # `subscription-userinfo` header has the real volume + expiry that
        # this bot's orders table stored as 0. We store the http link as
        # `sub_link` (so migration can read the header) and keep the vless
        # as `legacy_link` for display / fallback.
        _, link_raw = _first_nonempty(row, _LINK_COLUMNS)
        http_link, vless_link = _extract_links(link_raw)
        sub_link_val = http_link or vless_link          # prefer http for recovery
        legacy_link_val = vless_link or http_link         # keep vless for display
        # `legacy_link` is the de-dup key for re-import. To stay stable across
        # this code change (older imports may have stored a different element
        # of the array), we ALSO match existing subs by (user_id, remark).
        legacy_link = legacy_link_val or ""
        # Fallback dedup key: if the dump had no parseable VLESS/HTTP link for
        # this order, neither the legacy_link nor (often) the remark match would
        # fire on re-import, so the SAME order would insert a NEW duplicate sub
        # every single run. The legacy order `token` is guaranteed non-empty
        # (checked above) and unique per order, so use it as a stable sentinel
        # dedup key stored in legacy_link. This is only used when there's no
        # real link to display anyway.
        if not legacy_link and token:
            legacy_link = f"legacy-token:{token}"
            legacy_link_val = legacy_link

        # ── Volume + expiry resolution (the fix) ──────────────────────
        volume_col, volume_raw = _first_nonzero_int(row, _VOLUME_COLUMNS)
        volume_bytes = _normalize_volume_to_bytes(volume_raw)
        if volume_col:
            column_hits["volume"] += 1
            stats.orders_with_volume += 1
            if stats.volume_source_column is None:
                stats.volume_source_column = volume_col

        # One-shot diagnostic on the FIRST status=1 order: report which
        # column volume came from (or that NONE matched + the full row),
        # so a still-zero volume is instantly diagnosable from the logs.
        if not getattr(import_orders, "_logged_first_volume", False):
            import_orders._logged_first_volume = True  # type: ignore[attr-defined]
            if volume_col:
                logger.info(
                    "[VOLUME] detected from column %r: raw=%r → %d bytes (%.2f GB)",
                    volume_col, volume_raw, volume_bytes, volume_bytes / (1024**3),
                )
            else:
                logger.warning(
                    "[VOLUME] NO volume column matched on first order. "
                    "Row columns + values: %s",
                    {k: (str(v)[:40] if v is not None else None) for k, v in row.items()},
                )

        created_dt = _parse_unix(row.get("date")) or now
        expire_dt = _resolve_expiry(row, created_dt, now)
        if expire_dt is not None:
            column_hits["expire"] += 1
        else:
            # `_resolve_expiry` might have walked through _DAYS_COLUMNS;
            # we count whether days was specifically available.
            _, _days_v = _first_nonzero_int(row, _DAYS_COLUMNS)
            if _days_v > 0:
                column_hits["days"] += 1

        # Status: expired only if we actually KNOW it's past. If both
        # expire_date and days are missing we treat it as "active" so
        # the user at least sees the sub in their list and can migrate it.
        status = "expired" if (expire_dt is not None and expire_dt < now) else "active"

        legacy_remark_col, legacy_remark = _first_nonempty(row, _REMARK_COLUMNS)
        legacy_remark = legacy_remark[:128] or None

        # ── Idempotent UPDATE for already-imported rows ───────────────
        # Match an existing imported sub by legacy_link first, then fall
        # back to (user_id, legacy_remark). The remark fallback is what
        # keeps re-import stable now that we changed which link element
        # we store — older imports may have a different legacy_link, but
        # the remark (e.g. S3-1922655455-95027) is stable per config.
        existing_sub = None
        if legacy_link:
            existing_sub = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_uuid,
                    Subscription.legacy_link == legacy_link,
                    Subscription.source == "imported_legacy",
                )
            )
        if existing_sub is None and legacy_remark:
            existing_sub = await session.scalar(
                select(Subscription).where(
                    Subscription.user_id == user_uuid,
                    Subscription.legacy_remark == legacy_remark,
                    Subscription.source == "imported_legacy",
                )
            )
        if existing_sub is not None:
            changed = False
            # Fix zero volume.
            if existing_sub.volume_bytes == 0 and volume_bytes > 0:
                existing_sub.volume_bytes = volume_bytes
                changed = True
            # Upgrade sub_link to the http subscription URL (enables the
            # migration-time `subscription-userinfo` volume recovery).
            if sub_link_val and existing_sub.sub_link != sub_link_val and sub_link_val.startswith(("http://", "https://")):
                existing_sub.sub_link = sub_link_val
                changed = True
            # Fix missing expiry (and re-evaluate status).
            if existing_sub.ends_at is None and expire_dt is not None:
                existing_sub.ends_at = expire_dt
                if expire_dt > now and existing_sub.status == "expired":
                    existing_sub.status = "active"
                    existing_sub.expired_at = None
                elif expire_dt <= now and existing_sub.status == "active":
                    existing_sub.status = "expired"
                    existing_sub.expired_at = expire_dt
                changed = True
            # Fix missing remark.
            if not existing_sub.legacy_remark and legacy_remark:
                existing_sub.legacy_remark = legacy_remark
                changed = True
            if changed:
                try:
                    async with session.begin_nested():
                        await session.flush()
                    stats.orders_updated += 1
                except Exception as exc:
                    logger.warning("update for sub tg=%s remark=%s failed: %s",
                                   tg, legacy_remark, exc)
                    stats.orders_failed += 1
            else:
                stats.orders_skipped_duplicate += 1
            imported_tokens.add((tg, token))
            continue

        try:
            # SAVEPOINT per row so one bad sub doesn't wipe out earlier
            # successful flushes in the same transaction.
            async with session.begin_nested():
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
                    sub_link=sub_link_val or None,        # prefer http (recovery)
                    source="imported_legacy",
                    legacy_remark=legacy_remark,
                    legacy_link=legacy_link_val or None,  # vless for display
                )
                sub.created_at = created_dt
                # De-dup target is `legacy_link` (unique per legacy order
                # because each order has its own VLESS UUID). See
                # _existing_legacy_tokens.
                session.add(sub)
                await session.flush()
            imported_tokens.add((tg, token))
            stats.orders_inserted += 1
        except Exception as exc:
            logger.warning("order tg=%s token=%s failed: %s", tg, token, exc)
            stats.orders_failed += 1

    if stats.orders_seen:
        logger.info(
            "[ORDERS] column-detection summary — volume_found=%d, "
            "expire_date_found=%d, days_found=%d (of %d rows)",
            column_hits["volume"], column_hits["expire"], column_hits["days"],
            stats.orders_seen,
        )


def _uuid_from_vless(uri: str | None) -> str | None:
    """Pull the client UUID out of a vless:// URI: vless://<uuid>@host:port?…"""
    if not uri:
        return None
    m = re.match(r"^[a-z0-9]+://([^@]+)@", uri.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else None


async def _build_panel_client_index(session: AsyncSession) -> tuple[dict, dict, int]:
    """Scan every active X-UI server's inbounds ONCE and return two indexes
    keyed by client identity:
        by_email[email_lower] = (total_bytes, expiry_ms, enable)
        by_uuid[uuid]         = (total_bytes, expiry_ms, enable)
    plus the number of servers successfully scanned.
    """
    from sqlalchemy.orm import selectinload
    from models.xui import XUIServerRecord
    from services.xui.runtime import create_xui_client_for_server

    srv_rows = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.is_active.is_(True))
    )
    servers = list(srv_rows.scalars().all())
    by_email: dict[str, tuple[int | None, int | None, bool]] = {}
    by_uuid: dict[str, tuple[int | None, int | None, bool]] = {}
    scanned = 0

    for server in servers:
        try:
            async with create_xui_client_for_server(server) as client:
                inbounds = await client.get_inbounds()
        except Exception as exc:
            logger.warning("[PANEL-IDX] get_inbounds failed for server %s: %s", server.name, exc)
            continue
        scanned += 1
        indexed = 0
        for ib in inbounds:
            settings = ib.settings or {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:
                    settings = {}
            for c in (settings.get("clients") or []):
                email = str(c.get("email") or "").strip().lower()
                total = c.get("totalGB")
                expiry = c.get("expiryTime")
                enable = c.get("enable", True)
                triple = (
                    int(total) if total else None,
                    int(expiry) if expiry else None,
                    bool(enable),
                )
                if email:
                    by_email[email] = triple
                    indexed += 1
                cid = str(c.get("id") or c.get("uuid") or "").strip()
                if cid:
                    by_uuid[cid] = triple
            for cs in (ib.client_stats or []):
                email = str(cs.get("email") or "").strip().lower()
                if not email or email in by_email:
                    continue
                total = cs.get("total")
                expiry = cs.get("expiryTime")
                enable = cs.get("enable", True)
                by_email[email] = (
                    int(total) if total else None,
                    int(expiry) if expiry else None,
                    bool(enable),
                )
                indexed += 1
        logger.info("[PANEL-IDX] server %s — indexed %d clients", server.name, indexed)

    logger.info("[PANEL-IDX] index built: %d emails, %d uuids from %d servers",
                len(by_email), len(by_uuid), scanned)
    return by_email, by_uuid, scanned


def _apply_panel_state_to_sub(sub: "Subscription", hit: tuple, now: datetime) -> bool:
    """Apply (total, expiry_ms, enable) from the panel onto a sub. Returns
    True if anything changed. Panel is authoritative for volume + status +
    expiry of any config that exists on it."""
    total, expiry_ms, enable = hit
    changed = False
    if total and total > 0 and (sub.volume_bytes or 0) == 0:
        sub.volume_bytes = int(total)
        changed = True
    if not enable:
        if sub.status != "expired":
            sub.status = "expired"
            changed = True
    elif expiry_ms and expiry_ms > 0:
        try:
            new_end = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
            if sub.ends_at != new_end:
                sub.ends_at = new_end
                changed = True
            if new_end > now:
                if sub.status != "active":
                    sub.status = "active"
                    sub.expired_at = None
                    changed = True
            elif sub.status != "expired":
                sub.status = "expired"
                sub.expired_at = new_end
                changed = True
        except (OSError, OverflowError, ValueError):
            pass
    else:
        if sub.status != "active":
            sub.status = "active"
            sub.expired_at = None
            changed = True
        if sub.ends_at is not None and expiry_ms is not None and expiry_ms < 0:
            sub.ends_at = None
            changed = True
    return changed


def _match_sub(sub: "Subscription", by_email: dict, by_uuid: dict):
    key = str(sub.legacy_remark or "").strip().lower()
    hit = by_email.get(key)
    if hit is None:
        uid = _uuid_from_vless(sub.legacy_link)
        if uid:
            hit = by_uuid.get(uid)
    return hit


async def reconcile_imported_with_panel(session: AsyncSession, *, delete_missing: bool = True) -> dict:
    """One-shot full sync the operator asked for:
      * imported sub EXISTS on a panel → KEEP, sync volume/expiry/status.
      * imported sub NOT on any panel  → DELETE (ghost cleanup), but only
        if it has no XUIClientRecord (a real provisioned client is never
        auto-deleted).
    Returns {kept, updated, deleted, scanned_servers, total}.
    Commits in batches of 200.
    """
    by_email, by_uuid, scanned = await _build_panel_client_index(session)
    if scanned == 0:
        return {"kept": 0, "updated": 0, "deleted": 0, "scanned_servers": 0, "total": 0, "no_panel": True}

    rows = await session.execute(
        select(Subscription).where(Subscription.source == "imported_legacy")
    )
    subs = list(rows.scalars().all())
    now = datetime.now(timezone.utc)
    kept = updated = deleted = 0
    batch = 0

    for sub in subs:
        hit = _match_sub(sub, by_email, by_uuid)
        if hit is not None:
            kept += 1
            if _apply_panel_state_to_sub(sub, hit, now):
                updated += 1
                batch += 1
        elif delete_missing:
            has_client = await session.scalar(
                select(XUIClientRecord.id).where(XUIClientRecord.subscription_id == sub.id)
            )
            if has_client is None:
                await session.delete(sub)
                deleted += 1
                batch += 1
        if batch >= 200:
            await session.commit()
            batch = 0

    await session.commit()
    logger.info(
        "[RECONCILE] kept=%d updated=%d deleted=%d (scanned %d servers, %d subs)",
        kept, updated, deleted, scanned, len(subs),
    )
    return {"kept": kept, "updated": updated, "deleted": deleted,
            "scanned_servers": scanned, "total": len(subs)}


async def recover_volumes_from_panel(session: AsyncSession, stats: ImportStats, limit: int = 0) -> None:
    """Post-import pass: read the REAL volume + expiry straight off the
    operator's own X-UI (Sanaei) panels.

    This is the authoritative source: the legacy bot stored volume=0 in
    its DB and tracked quota only on the panel. The configs already live
    on the X-UI servers configured in THIS bot, so we list every active
    server's inbounds ONCE, build an index keyed by the client's email
    (== the legacy remark, e.g. S3-1922655455-95027) and by UUID, then
    fill in volume_bytes + ends_at for every imported sub that's still 0.

    Reading get_inbounds once per server (not once per sub) keeps it fast
    even on panels with thousands of clients.
    """
    from sqlalchemy.orm import selectinload
    from models.xui import XUIServerRecord
    from services.xui.runtime import create_xui_client_for_server

    # Process ALL imported subs (not just volume==0): the panel is also
    # authoritative for status/expiry, and configs that already got their
    # volume on a prior run may still carry the stale dump status.
    rows = await session.execute(
        select(Subscription).where(
            Subscription.source == "imported_legacy",
            Subscription.legacy_remark.is_not(None),
        )
    )
    targets: list[Subscription] = list(rows.scalars().all())
    if limit:
        targets = targets[:limit]
    if not targets:
        logger.info("[PANEL-VOL] no imported subs with a remark to recover")
        return

    srv_rows = await session.execute(
        select(XUIServerRecord)
        .options(selectinload(XUIServerRecord.credentials))
        .where(XUIServerRecord.is_active.is_(True))
    )
    servers = list(srv_rows.scalars().all())
    if not servers:
        logger.warning("[PANEL-VOL] no active X-UI servers configured — can't read volume from panel")
        return

    # Build a combined index from EVERY server's inbounds:
    #   by_email[email_lower] = (total_bytes, expiry_ms)
    #   by_uuid[uuid]         = (total_bytes, expiry_ms)
    by_email: dict[str, tuple[int | None, int | None]] = {}
    by_uuid: dict[str, tuple[int | None, int | None]] = {}

    for server in servers:
        stats.sublink_fetch_attempts += 1
        try:
            async with create_xui_client_for_server(server) as client:
                inbounds = await client.get_inbounds()
        except Exception as exc:
            logger.warning("[PANEL-VOL] get_inbounds failed for server %s: %s", server.name, exc)
            continue

        indexed = 0
        for ib in inbounds:
            # settings.clients carries totalGB (bytes) + expiryTime + id(uuid)
            # + enable. This is the authoritative per-client record.
            settings = ib.settings or {}
            if isinstance(settings, str):
                try:
                    settings = json.loads(settings)
                except Exception:
                    settings = {}
            for c in (settings.get("clients") or []):
                email = str(c.get("email") or "").strip().lower()
                total = c.get("totalGB")
                expiry = c.get("expiryTime")
                enable = c.get("enable", True)
                triple = (
                    int(total) if total else None,
                    int(expiry) if expiry else None,
                    bool(enable),
                )
                if email:
                    by_email[email] = triple
                    indexed += 1
                cid = str(c.get("id") or c.get("uuid") or "").strip()
                if cid:
                    by_uuid[cid] = triple
            # clientStats is a fallback for panels that don't expose clients
            # in settings — it has email, total (limit), expiryTime, enable.
            for cs in (ib.client_stats or []):
                email = str(cs.get("email") or "").strip().lower()
                if not email or email in by_email:
                    continue
                total = cs.get("total")
                expiry = cs.get("expiryTime")
                enable = cs.get("enable", True)
                by_email[email] = (
                    int(total) if total else None,
                    int(expiry) if expiry else None,
                    bool(enable),
                )
                indexed += 1
        logger.info("[PANEL-VOL] server %s — indexed %d clients", server.name, indexed)

    logger.info(
        "[PANEL-VOL] index built: %d emails, %d uuids; matching %d subs",
        len(by_email), len(by_uuid), len(targets),
    )

    now = datetime.now(timezone.utc)
    first_logged = False
    processed_since_commit = 0
    for sub in targets:
        # Match by remark→email first, then by UUID from the vless link.
        key = str(sub.legacy_remark or "").strip().lower()
        hit = by_email.get(key)
        if hit is None:
            uid = _uuid_from_vless(sub.legacy_link)
            if uid:
                hit = by_uuid.get(uid)
        if hit is None:
            continue
        total, expiry_ms, enable = hit
        if not first_logged:
            first_logged = True
            logger.info("[PANEL-VOL] first match — remark=%s total=%s expiry_ms=%s enable=%s",
                        sub.legacy_remark, total, expiry_ms, enable)
        changed = False

        # Volume from the panel.
        if total and total > 0 and sub.volume_bytes == 0:
            sub.volume_bytes = int(total)
            changed = True

        # The PANEL is authoritative for status + expiry of any config
        # that exists on it — the dump's expire_date is stale (it's the
        # ORIGINAL order expiry, before renewals). So override:
        #   * disabled on panel        → keep/mark expired
        #   * expiryTime > 0 (absolute) → active if future, else expired
        #   * expiryTime <= 0 / None   → not-yet-used or unlimited → active
        if not enable:
            if sub.status != "expired":
                sub.status = "expired"
                changed = True
        elif expiry_ms and expiry_ms > 0:
            try:
                new_end = datetime.fromtimestamp(expiry_ms / 1000, tz=timezone.utc)
                if sub.ends_at != new_end:
                    sub.ends_at = new_end
                    changed = True
                if new_end > now:
                    if sub.status != "active":
                        sub.status = "active"
                        sub.expired_at = None
                        changed = True
                else:
                    if sub.status != "expired":
                        sub.status = "expired"
                        sub.expired_at = new_end
                        changed = True
            except (OSError, OverflowError, ValueError):
                pass
        else:
            # Enabled + relative/zero expiry → it's a live, not-yet-expired
            # config. Mark active and clear the stale dump end date.
            if sub.status != "active":
                sub.status = "active"
                sub.expired_at = None
                changed = True
            if sub.ends_at is not None and expiry_ms is not None and expiry_ms < 0:
                # relative (first-use) expiry — no fixed end date yet
                sub.ends_at = None
                changed = True

        if changed:
            stats.volume_recovered_from_panel += 1
            processed_since_commit += 1
            # Commit in batches so progress is durable and the session's
            # transaction doesn't balloon into a single giant final commit
            # (which is what froze the bot on big imports).
            if processed_since_commit >= 200:
                await session.commit()
                processed_since_commit = 0

    await session.commit()
    logger.info(
        "[PANEL-VOL] recovered volume/expiry for %d of %d subs (scanned %d servers)",
        stats.volume_recovered_from_panel, len(targets), stats.sublink_fetch_attempts,
    )


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


async def dedupe_imported_subs(session: AsyncSession, stats: ImportStats) -> None:
    """Remove duplicate imported_legacy subs created by earlier buggy
    re-imports (when the de-dup key didn't match across code versions).

    Keep ONE sub per (user_id, legacy_remark) — the oldest by created_at,
    preferring one that already has a non-zero volume. Delete the rest.
    Imported ghost subs have no XUIClientRecord, so deletion is safe.
    """
    from collections import defaultdict
    rows = await session.execute(
        select(Subscription).where(Subscription.source == "imported_legacy")
    )
    subs = list(rows.scalars().all())
    groups: dict[tuple, list[Subscription]] = defaultdict(list)
    for s in subs:
        key = (s.user_id, (s.legacy_remark or "").strip().lower())
        if not key[1]:
            continue  # no remark → can't safely group; leave it alone
        groups[key].append(s)

    deleted = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        # Keep the best: prefer non-zero volume, then earliest created_at.
        members.sort(key=lambda s: (
            0 if (s.volume_bytes or 0) > 0 else 1,
            s.created_at or datetime.now(timezone.utc),
        ))
        keep = members[0]
        for victim in members[1:]:
            # Only delete true ghosts (no xui client mapping).
            has_client = await session.scalar(
                select(XUIClientRecord.id).where(XUIClientRecord.subscription_id == victim.id)
            )
            if has_client is not None:
                continue
            await session.delete(victim)
            deleted += 1
        # If kept has zero volume but a victim had volume, copy it over.
        if (keep.volume_bytes or 0) == 0:
            for m in members[1:]:
                if (m.volume_bytes or 0) > 0:
                    keep.volume_bytes = m.volume_bytes
                    if keep.ends_at is None and m.ends_at is not None:
                        keep.ends_at = m.ends_at
                    break

    if deleted:
        await session.commit()
        logger.info("[DEDUPE] removed %d duplicate imported subs", deleted)
    stats.orders_deduped = deleted


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

    # ── Schema diagnostics ───────────────────────────────────────────────
    # Print the FULL column list + a sample row of orders_list so we can see
    # exactly what the legacy dump calls each field. When volume lands at 0
    # after an import, this is what tells us which column actually holds the
    # quota (so we can add it to _VOLUME_COLUMNS if it's a name we don't
    # already recognise).
    if "orders_list" in by_table:
        cols, rows = by_table["orders_list"]
        logger.info("[SCHEMA] orders_list columns (%d): %s", len(cols), ", ".join(cols))
        # Find the first row whose status==1 (a real provisioned order) for
        # the sample, falling back to the very first row.
        sample = None
        try:
            status_idx = cols.index("status") if "status" in cols else None
        except ValueError:
            status_idx = None
        for r in rows:
            if status_idx is not None and status_idx < len(r):
                if str(r[status_idx]).strip() == "1":
                    sample = r
                    break
        if sample is None and rows:
            sample = rows[0]
        if sample is not None:
            sample_dict = dict(zip(cols, sample))
            # Truncate long values (e.g. the vless link JSON) so the log
            # stays readable.
            preview = {
                k: (str(v)[:60] + "…" if v is not None and len(str(v)) > 60 else v)
                for k, v in sample_dict.items()
            }
            logger.info("[SCHEMA] orders_list sample row: %s", preview)

    stats = ImportStats()
    # Stash the orders_list schema on stats so the bot summary can show it.
    if "orders_list" in by_table:
        _cols, _rows = by_table["orders_list"]
        stats.orders_columns = list(_cols)
        _smp = None
        try:
            _sidx = _cols.index("status") if "status" in _cols else None
        except ValueError:
            _sidx = None
        for _r in _rows:
            if _sidx is not None and _sidx < len(_r) and str(_r[_sidx]).strip() == "1":
                _smp = _r
                break
        if _smp is None and _rows:
            _smp = _rows[0]
        if _smp is not None:
            stats.orders_sample_row = {
                k: (str(v)[:48] if v is not None else None)
                for k, v in dict(zip(_cols, _smp)).items()
            }

    async with AsyncSessionFactory() as session:
        toman_rate = await AppSettingsRepository(session).get_toman_rate()
        logger.info("Using USD→Toman rate %s for wallet conversion", f"{toman_rate:,}")

        # Users first; orders depend on Users existing.
        if "users" in by_table:
            cols, rows = by_table["users"]
            await import_users(session, rows_as_dicts(cols, rows), toman_rate, stats, limit=limit)
            if not dry_run:
                await session.commit()  # phase 1 durable

        if "orders_list" in by_table:
            cols, rows = by_table["orders_list"]
            await import_orders(session, rows_as_dicts(cols, rows), stats, limit=limit)
            if not dry_run:
                await session.commit()  # phase 2 durable — imported subs saved

        if dry_run:
            await session.rollback()
            logger.info("DRY RUN — rolled back, no DB changes committed.")
            return stats

        # Phase 3: clean up duplicate ghost subs from earlier re-imports.
        try:
            await dedupe_imported_subs(session, stats)
        except Exception as exc:
            logger.warning("dedupe pass failed: %s", exc, exc_info=True)
            await session.rollback()

        # Phase 4: read the REAL volume + expiry off the operator's X-UI
        # panel(s) and write it into the imported subs. Commits in batches
        # internally so a big import can't balloon into one giant final
        # commit (the cause of the bot "freeze").
        try:
            await recover_volumes_from_panel(session, stats, limit=limit)
        except Exception as exc:
            logger.warning("panel volume recovery pass failed: %s", exc, exc_info=True)
            try:
                await session.commit()  # save whatever recovery managed before the error
            except Exception:
                await session.rollback()

        # ── Cumulative DB state — the honest picture across all runs ────
        try:
            _active = Subscription.status.in_(("active", "pending_activation"))
            stats.imported_total = int(await session.scalar(
                select(func.count(Subscription.id)).where(Subscription.source == "imported_legacy")
            ) or 0)
            stats.imported_with_volume = int(await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.source == "imported_legacy",
                    Subscription.volume_bytes > 0,
                )
            ) or 0)
            stats.imported_active = int(await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.source == "imported_legacy", _active
                )
            ) or 0)
            stats.imported_active_no_volume = int(await session.scalar(
                select(func.count(Subscription.id)).where(
                    Subscription.source == "imported_legacy", _active,
                    Subscription.volume_bytes == 0,
                )
            ) or 0)
        except Exception as exc:
            logger.warning("final DB-state count failed: %s", exc)

        logger.info("Committed (all phases).")
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
    logger.info(
        "  Orders | seen=%d  inserted=%d  updated_in_place=%d  skipped(duplicate)=%d  failed=%d",
        stats.orders_seen, stats.orders_inserted, stats.orders_updated,
        stats.orders_skipped_duplicate, stats.orders_failed,
    )
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Restart bot containers so model code sees the new sub rows.")
    logger.info("  2. Tell imported users to /start — they'll see their legacy")
    logger.info("     configs in the bot, with a CTA to migrate to a new inbound")
    logger.info("     (the migration flow preserves the original config name).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
