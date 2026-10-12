"""
Mini-app QR rendering endpoint.

`GET /api/miniapp/qr?data=...` renders the given payload as a QR PNG
locally (segno via core.qr.make_qr_bytes). The mini-app used to embed
api.qrserver.com <img> URLs, which shipped the full vless:// credential
(client UUID + server host) to a third party on every config-detail
open — rendering here keeps credentials inside our own trust boundary
and keeps working when external domains are filtered.

Auth matches every other mini-app endpoint. The frontend sends headers on
POST, then displays a local blob URL; credentials never enter access-log URLs.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from apps.api.routes.miniapp.users import _get_current_user
from core.qr import make_qr_bytes
from models.user import User

router = APIRouter()

# vless:// URIs and signed sub_links are a few hundred chars; anything far
# beyond that won't fit a scannable QR anyway, so cap the payload early.
MAX_QR_PAYLOAD_CHARS = 2048


@router.get("/qr")
async def render_qr(
    data: str = "",
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> Response:
    payload = data.strip()
    if not payload or len(payload) > MAX_QR_PAYLOAD_CHARS:
        raise HTTPException(status_code=400, detail="داده QR نامعتبر است.")
    png = make_qr_bytes(payload)
    if not png:
        # segno missing or payload too dense — make_qr_bytes returns b"".
        raise HTTPException(status_code=503, detail="ساخت QR در حال حاضر ممکن نیست.")
    return Response(
        content=png,
        media_type="image/png",
        # The URL carries credentials (_auth + the config payload) — never
        # let intermediaries or the webview disk-cache this response.
        headers={"Cache-Control": "no-store"},
    )


class QRRequest(BaseModel):
    data: str = Field(min_length=1, max_length=MAX_QR_PAYLOAD_CHARS)

@router.post("/qr")
async def render_qr_post(body: QRRequest, auth: tuple[User, AsyncSession] = Depends(_get_current_user)) -> Response:
    return await render_qr(data=body.data, auth=auth)
