"""
Dashboard auth endpoints — POST /login, POST /logout, GET /me.

Rate-limited internally: 5 wrong logins per username in a 5-minute
window get a 429 with a Retry-After hint. This is in-memory only
(per-process), good enough for a single-uvicorn dashboard and zero-
infrastructure to set up. Distributed rate-limiting would need Redis.

/logout revokes server-side (bumps the admin's auth epoch) rather than
merely clearing the cookie — see the handler for why it stays
unauthenticated while still doing so.
"""
from __future__ import annotations

import logging
import asyncio
import hashlib
import time
from collections import deque
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.dashboard._deps import (
    SessionDep,
    require_dashboard_admin,
)
from core.dashboard_auth import (
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_OPTS,
    SESSION_TTL_SECONDS,
    hash_password,
    issue_session,
    needs_rehash,
    verify_password,
    verify_session,
)
from models.dashboard_admin import DashboardAdmin
from core.rate_limit import check_rate_limit
from starlette.concurrency import run_in_threadpool


logger = logging.getLogger(__name__)
router = APIRouter()


# ── In-memory rate-limit on /login (per-process) ─────────────────────────
# Keys are attacker-controlled (any unauthenticated POST body), so the dict
# is pruned on every check and hard-capped to stay bounded in memory.
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_LOGIN_FAIL_WINDOW = 300  # seconds
_LOGIN_FAIL_THRESHOLD = 5
_LOGIN_FAILURES_MAX_KEYS = 10_000  # hard cap on tracked usernames
_PASSWORD_SLOTS = asyncio.Semaphore(2)


def _prune_login_failures(now: float) -> None:
    """Drop timestamps older than the window and delete empty buckets.

    Without this, every distinct username ever submitted would keep a dict
    entry forever (slow memory-exhaustion DoS from unauthenticated requests).
    """
    cutoff = now - _LOGIN_FAIL_WINDOW
    for key in list(_LOGIN_FAILURES):
        bucket = _LOGIN_FAILURES[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if not bucket:
            del _LOGIN_FAILURES[key]


def _check_login_rate(username_key: str) -> int:
    """Return 0 if allowed, else seconds-to-wait."""
    now = time.time()
    _prune_login_failures(now)
    bucket = _LOGIN_FAILURES.get(username_key)
    if bucket and len(bucket) >= _LOGIN_FAIL_THRESHOLD:
        retry_after = int(_LOGIN_FAIL_WINDOW - (now - bucket[0])) + 1
        return max(retry_after, 1)
    return 0


def _record_login_failure(username_key: str) -> None:
    # Safety valve: if pruning alone can't keep the dict under the cap
    # (burst of distinct usernames inside one window), evict the
    # oldest-inserted keys before adding a new one.
    if username_key not in _LOGIN_FAILURES:
        while len(_LOGIN_FAILURES) >= _LOGIN_FAILURES_MAX_KEYS:
            del _LOGIN_FAILURES[next(iter(_LOGIN_FAILURES))]
    _LOGIN_FAILURES.setdefault(username_key, deque()).append(time.time())


def _clear_login_failures(username_key: str) -> None:
    _LOGIN_FAILURES.pop(username_key, None)


# ── Pydantic schemas ─────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class AdminProfile(BaseModel):
    id: str
    username: str
    display_name: str | None
    last_login_at: datetime | None
    is_active: bool


# ── Endpoints ────────────────────────────────────────────────────────────
@router.post("/login")
async def login(body: LoginIn, response: Response, session: SessionDep, request: Request) -> dict[str, Any]:
    username_key = body.username.strip().lower()
    if not username_key:
        raise HTTPException(status_code=400, detail="نام کاربری معتبر نیست.")

    # request.client is the peer after Uvicorn's trusted-proxy processing;
    # never accept an arbitrary X-Forwarded-For header here.
    peer = request.client.host if request.client else "unknown"
    await check_rate_limit(peer, "dashboard_login_ip", max_requests=20, window_seconds=300)
    await check_rate_limit("global", "dashboard_login_global", max_requests=100, window_seconds=60)
    account_key = hashlib.sha256(username_key.encode()).hexdigest()
    await check_rate_limit(account_key, "dashboard_login_account", max_requests=10, window_seconds=300)

    retry_after = _check_login_rate(username_key)
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"تلاش‌های ناموفق زیاد. لطفاً {retry_after} ثانیه صبر کنید.",
            headers={"Retry-After": str(retry_after)},
        )

    admin = await session.scalar(
        select(DashboardAdmin).where(
            func.lower(DashboardAdmin.username) == username_key,
        )
    )
    # NOTE: we always run verify_password — even if `admin is None` — so
    # the response time doesn't leak whether the username exists.
    dummy_hash = "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    target_hash = admin.password_hash if admin else dummy_hash
    async with _PASSWORD_SLOTS:
        ok = await run_in_threadpool(verify_password, body.password, target_hash)
    if admin is None or not admin.is_active or not ok:
        _record_login_failure(username_key)
        raise HTTPException(status_code=401, detail="نام کاربری یا رمز عبور نادرست است.")

    # Transparent upgrade if params changed since this hash was minted.
    if needs_rehash(admin.password_hash):
        async with _PASSWORD_SLOTS:
            admin.password_hash = await run_in_threadpool(hash_password, body.password)
    admin.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    _clear_login_failures(username_key)
    logger.info("Dashboard login OK admin=%s username=%s", admin.id, admin.username)

    token = issue_session(admin.id, session_version=int(admin.session_version or 0))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        **SESSION_COOKIE_OPTS,
    )
    return {
        "ok": True,
        "admin": AdminProfile(
            id=str(admin.id),
            username=admin.username,
            display_name=admin.display_name,
            last_login_at=admin.last_login_at,
            is_active=admin.is_active,
        ).model_dump(),
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    session: SessionDep,
) -> dict[str, bool]:
    # Deleting the cookie only asks the browser to forget the token — the
    # token itself stays valid until it expires, so anyone who captured it
    # keeps access. Bumping the admin's auth epoch is what actually revokes
    # it, here and on every other device.
    #
    # Deliberately NOT behind require_dashboard_admin: the SPA calls this
    # unconditionally (including after a 401), and a logout that can itself
    # fail with 401 would strand the user with a cookie they can't clear.
    # Every failure path below therefore degrades to "just drop the cookie".
    token = request.cookies.get(SESSION_COOKIE_NAME)
    payload = verify_session(token or "") if token else None
    if payload is not None:
        try:
            admin = await session.get(DashboardAdmin, payload.admin_id)
            if admin is not None:
                admin.session_version = int(admin.session_version or 0) + 1
                await session.commit()
                logger.info("Dashboard logout admin=%s (sessions revoked)", admin.id)
        except Exception:
            # A DB hiccup must not block the sign-out gesture.
            logger.exception("Dashboard logout could not bump session epoch")
            try:
                await session.rollback()
            except Exception:
                pass

    # Wipe the cookie. SameSite/Path/Secure must match the issuing call
    # or some browsers won't drop it.
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path=SESSION_COOKIE_OPTS["path"],
        samesite=SESSION_COOKIE_OPTS["samesite"],
        secure=SESSION_COOKIE_OPTS["secure"],
    )
    return {"ok": True}


@router.get("/me")
async def me(
    auth: Annotated[
        tuple[DashboardAdmin, AsyncSession],
        Depends(require_dashboard_admin),
    ],
) -> dict[str, Any]:
    admin, _ = auth
    return AdminProfile(
        id=str(admin.id),
        username=admin.username,
        display_name=admin.display_name,
        last_login_at=admin.last_login_at,
        is_active=admin.is_active,
    ).model_dump()
