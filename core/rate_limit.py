"""
Redis-based rate limiter for FastAPI endpoints.
Uses sliding window counter pattern.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from functools import wraps
from typing import Any, Callable, Deque

from fastapi import HTTPException, Request, status

from core.redis import get_redis

logger = logging.getLogger(__name__)

# Default rate limits per endpoint group (requests, window_seconds)
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "purchase": (5, 60),        # 5 purchases per minute
    "topup": (5, 60),           # 5 topups per minute
    "renewal": (5, 60),         # 5 renewals per minute
    "ticket": (3, 60),          # 3 tickets per minute
    "ticket_msg": (10, 60),     # 10 ticket messages per minute
    "admin_action": (30, 60),   # 30 admin actions per minute
    "default": (60, 60),        # 60 requests per minute default
}


# In-memory fallback used only when Redis is unreachable. Limits are tightened
# (halved) on this path so a flapping Redis cannot be exploited as an
# amplifier — but the endpoint stays available for legitimate users.
_FALLBACK_HITS: dict[str, Deque[float]] = defaultdict(deque)


def _fallback_allow(key: str, max_req: int, window: int) -> tuple[bool, int]:
    now = time.monotonic()
    bucket = _FALLBACK_HITS[key]
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    # Tighter limit on the degraded path.
    degraded_max = max(1, max_req // 2)
    if len(bucket) >= degraded_max:
        ttl = max(1, int(window - (now - bucket[0])))
        return False, ttl
    bucket.append(now)
    return True, 0


async def check_rate_limit(
    user_id: int | str,
    action: str,
    *,
    max_requests: int | None = None,
    window_seconds: int | None = None,
) -> None:
    """Check rate limit for a user/action combination. Raises 429 if exceeded."""
    limits = RATE_LIMITS.get(action, RATE_LIMITS["default"])
    max_req = max_requests or limits[0]
    window = window_seconds or limits[1]

    redis = get_redis()
    key = f"ratelimit:{action}:{user_id}"

    try:
        current = await redis.incr(key)
        # Attach a TTL whenever the key has none (nx=True). The old code only set
        # the TTL when current==1, so if the process died between INCR and EXPIRE
        # the key was orphaned with NO TTL and blocked the user forever. Running
        # expire(nx=True) every call self-heals such an orphan on the next request
        # while never extending an existing window.
        await redis.expire(key, window, nx=True)

        if current > max_req:
            ttl = await redis.ttl(key)
            logger.warning(
                "Rate limit exceeded: user=%s action=%s count=%d limit=%d",
                user_id, action, current, max_req,
            )
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"تعداد درخواست‌ها بیش از حد مجاز است. لطفاً {ttl} ثانیه صبر کنید.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        # Redis is down — degrade to a tight in-memory limiter rather than
        # failing open. Spikes still get rejected; legitimate traffic survives.
        logger.error("Rate limit check failed for user=%s action=%s: %s", user_id, action, exc)
        allowed, ttl = _fallback_allow(key, max_req, window)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"تعداد درخواست‌ها بیش از حد مجاز است. لطفاً {ttl} ثانیه صبر کنید.",
            )
