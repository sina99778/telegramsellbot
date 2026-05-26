"""
Dashboard auth endpoints — POST /login, POST /logout, GET /me.

Rate-limited internally: 5 wrong logins per username in a 5-minute
window get a 429 with a Retry-After hint. This is in-memory only
(per-process), good enough for a single-uvicorn dashboard and zero-
infrastructure to set up. Distributed rate-limiting would need Redis.
"""
from __future__ import annotations

import logging
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
)
from models.dashboard_admin import DashboardAdmin


logger = logging.getLogger(__name__)
router = APIRouter()


# ── In-memory rate-limit on /login (per-process) ─────────────────────────
_LOGIN_FAILURES: dict[str, deque[float]] = {}
_LOGIN_FAIL_WINDOW = 300  # seconds
_LOGIN_FAIL_THRESHOLD = 5


def _check_login_rate(username_key: str) -> int:
    """Return 0 if allowed, else seconds-to-wait."""
    now = time.time()
    bucket = _LOGIN_FAILURES.setdefault(username_key, deque())
    while bucket and bucket[0] < now - _LOGIN_FAIL_WINDOW:
        bucket.popleft()
    if len(bucket) >= _LOGIN_FAIL_THRESHOLD:
        retry_after = int(_LOGIN_FAIL_WINDOW - (now - bucket[0])) + 1
        return max(retry_after, 1)
    return 0


def _record_login_failure(username_key: str) -> None:
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
async def login(body: LoginIn, response: Response, session: SessionDep) -> dict[str, Any]:
    username_key = body.username.strip().lower()
    if not username_key:
        raise HTTPException(status_code=400, detail="نام کاربری معتبر نیست.")

    retry_after = _check_login_rate(username_key)
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
        raise HTTPException(
            status_code=429,
            detail=f"تلاش‌های ناموفق زیاد. لطفاً {retry_after} ثانیه صبر کنید.",
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
    ok = verify_password(body.password, target_hash)
    if admin is None or not admin.is_active or not ok:
        _record_login_failure(username_key)
        raise HTTPException(status_code=401, detail="نام کاربری یا رمز عبور نادرست است.")

    # Transparent upgrade if params changed since this hash was minted.
    if needs_rehash(admin.password_hash):
        admin.password_hash = hash_password(body.password)
    admin.last_login_at = datetime.now(timezone.utc)
    await session.commit()
    _clear_login_failures(username_key)
    logger.info("Dashboard login OK admin=%s username=%s", admin.id, admin.username)

    token = issue_session(admin.id)
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
async def logout(response: Response) -> dict[str, bool]:
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
