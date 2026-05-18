from __future__ import annotations

import base64
import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select

from core.config import settings
from core.database import AsyncSessionFactory
from core.miniapp_auth import verify_subscription_signature
from models.ready_config import ReadyConfigItem
from models.subscription import Subscription

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/sub/{sub_id}")
async def get_subscription_content(
    sub_id: UUID,
    sig: str | None = Query(default=None, min_length=8, max_length=64),
):
    """Returns the base64 encoded subscription content for ready-config items.

    Access is gated by an HMAC signature bound to the subscription UUID so
    the endpoint cannot be enumerated by guessing UUIDs.

    Compatibility: subscriptions created before the signature rollout have
    sub_link rows in the DB that lack `?sig=`. While
    `settings.sub_legacy_unsigned_access` is True we still serve those
    requests (with a warning log so operators can monitor adoption) — but
    only if the subscription actually exists in our DB. Once usage of
    unsigned links drops to zero, flip the setting to False to enforce
    strict signing.
    """
    valid_sig = verify_subscription_signature(str(sub_id), sig)

    if not valid_sig and not settings.sub_legacy_unsigned_access:
        # Strict mode: indistinguishable from "not found" so an attacker
        # can't tell whether a UUID exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")

    async with AsyncSessionFactory() as session:
        subscription = await session.get(Subscription, sub_id)
        if not subscription:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")

        if not valid_sig:
            # Grace mode: subscription exists, sig is missing/wrong. Log
            # so we can tell when it's safe to flip the flag off.
            logger.warning(
                "sub.py: legacy unsigned access to sub_id=%s (enable strict mode after migration)",
                sub_id,
            )

        item = await session.scalar(
            select(ReadyConfigItem)
            .where(ReadyConfigItem.subscription_id == sub_id)
            .limit(1)
        )

        if item:
            vless_uri = item.content.split("|")[0].strip()
            content_bytes = vless_uri.encode("utf-8")
            b64_content = base64.b64encode(content_bytes).decode("utf-8")
            return PlainTextResponse(b64_content)

        if subscription.sub_link and subscription.sub_link.startswith("http") and "/api/sub/" not in subscription.sub_link:
            return RedirectResponse(subscription.sub_link)

        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuration content not found")
