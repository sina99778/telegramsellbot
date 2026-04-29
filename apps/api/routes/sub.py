from uuid import UUID
import base64

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from core.database import AsyncSessionFactory
from models.subscription import Subscription
from models.ready_config import ReadyConfigItem

router = APIRouter()

@router.get("/sub/{sub_id}")
async def get_subscription_content(sub_id: UUID):
    """
    Returns the base64 encoded subscription content.
    For ready configs, this encodes the raw config string (e.g. vless://...)
    and returns it so clients can update their subscriptions.
    """
    async with AsyncSessionFactory() as session:
        # Check if the subscription exists
        subscription = await session.get(Subscription, sub_id)
        if not subscription:
            raise HTTPException(status_code=404, detail="Subscription not found")
        
        # Check if there is an associated ready config item
        item = await session.scalar(
            select(ReadyConfigItem)
            .where(ReadyConfigItem.subscription_id == sub_id)
            .limit(1)
        )
        
        if item:
            # It is a ready config. Encode the content in Base64
            # If the content has a '|', the first part is the vless_uri
            vless_uri = item.content.split("|")[0].strip()
            content_bytes = vless_uri.encode("utf-8")
            b64_content = base64.b64encode(content_bytes).decode("utf-8")
            return PlainTextResponse(b64_content)
        
        # If it's a regular X-UI config but they hit this endpoint by mistake,
        # redirect to the actual X-UI sub_link.
        if subscription.sub_link and subscription.sub_link.startswith("http") and "/api/sub/" not in subscription.sub_link:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(subscription.sub_link)

        raise HTTPException(status_code=404, detail="Configuration content not found")
