"""
Redis-based rate limiter for FastAPI endpoints.
Uses sliding window counter pattern.
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable

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
        if current == 1:
            await redis.expire(key, window)

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
        # If Redis is down, allow the request (fail-open)
        logger.warning("Rate limit check failed (allowing request): %s", exc)
