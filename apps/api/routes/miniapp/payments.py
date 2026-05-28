"""
Mini-app payment helpers: TXID submission for manual crypto and
receipt-photo upload for card-to-card top-ups.

Faoxima parity for `api/handlers/CryptoSubmitHashHandler.php` and
`api/handlers/PaymentReceiptHandler.php`. Lets the user complete a
manual payment without leaving the mini-app.

    POST   /api/miniapp/payments/crypto_hash   — submit TXID for a
                                                 pending manual_crypto
                                                 invoice
    POST   /api/miniapp/payments/card_receipt  — upload a JPEG/PNG
                                                 receipt for a pending
                                                 card_to_card top-up
    GET    /api/miniapp/payments/pending       — list user's pending
                                                 payments (so the UI
                                                 can show a "Resume
                                                 payment" CTA)

Auth via the same `_get_current_user` dependency the rest of the
mini-app uses (Telegram initData + signed-cookie fallback).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.routes.miniapp.users import _get_current_user
from core.config import settings
from models.payment import Payment
from models.user import User


logger = logging.getLogger(__name__)
router = APIRouter()


# ─── List pending payments ─────────────────────────────────────────


@router.get("/payments/pending")
async def list_pending_payments(
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    """User's open payments — anything not in a terminal state."""
    user, session = auth
    rows = (await session.execute(
        select(Payment)
        .where(
            Payment.user_id == user.id,
            Payment.payment_status.in_((
                "waiting", "waiting_hash", "waiting_receipt", "pending_approval",
            )),
        )
        .order_by(desc(Payment.created_at))
        .limit(20)
    )).scalars().all()
    items: list[dict[str, Any]] = []
    for p in rows:
        items.append({
            "id": str(p.id),
            "provider": p.provider,
            "kind": p.kind,
            "status": p.payment_status,
            "price_amount_usd": float(p.price_amount or 0),
            "pay_amount": float(p.pay_amount or 0),
            "pay_currency": p.pay_currency,
            "invoice_url": p.invoice_url,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        })
    return {"items": items, "total": len(items)}


# ─── Manual crypto: submit TXID ────────────────────────────────────


class CryptoHashBody(BaseModel):
    payment_id: UUID
    tx_hash: str = Field(..., min_length=4, max_length=200)


@router.post("/payments/crypto_hash")
async def submit_crypto_hash(
    body: CryptoHashBody,
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    """User-side TXID submission for a pending manual-crypto invoice.

    The worker job `run_crypto_autoconfirm` already scans incoming TXs
    against pending invoices, so a user-submitted TXID is treated as a
    HINT — we stamp it on the payment so the operator (or the worker)
    can correlate quickly, and we DO NOT credit the wallet directly.
    Credit goes through the regular auto-confirm code path once the
    on-chain TX is confirmed.
    """
    user, session = auth
    payment = await session.scalar(
        select(Payment).where(Payment.id == body.payment_id).with_for_update()
    )
    if payment is None or payment.user_id != user.id:
        raise HTTPException(status_code=404, detail="payment not found")
    if payment.provider != "manual_crypto":
        raise HTTPException(status_code=400, detail="payment is not a manual crypto invoice")
    if payment.payment_status not in {"waiting_hash", "waiting"}:
        raise HTTPException(
            status_code=400,
            detail=f"payment already processed (status={payment.payment_status})",
        )

    cleaned = body.tx_hash.strip()
    payload = dict(payment.callback_payload or {})
    payload["user_submitted_tx_hash"] = cleaned
    payload["user_submitted_at"] = datetime.now(timezone.utc).isoformat()
    payment.callback_payload = payload
    payment.provider_payment_id = cleaned
    await session.commit()

    return {
        "ok": True,
        "message": "TXID ثبت شد. به محض تأیید روی بلاکچین، کیف پول شما به‌صورت خودکار شارژ می‌شود.",
    }


# ─── Card-to-card: upload receipt photo ────────────────────────────


_MAX_RECEIPT_BYTES = 6 * 1024 * 1024  # 6 MB — Telegram's photo size cap


@router.post("/payments/card_receipt")
async def upload_card_receipt(
    payment_id: UUID = Form(...),
    photo: UploadFile = File(...),
    auth: tuple[User, AsyncSession] = Depends(_get_current_user),
) -> dict[str, Any]:
    """User uploads a receipt photo through the mini-app.

    We push the photo to Telegram via the bot's `sendPhoto` API — this
    gives us a `file_id` we can stamp on the Payment, matching exactly
    the format the bot-side card-receipt handler stores. The admin
    notification message (sent to admin telegram_id's with approve/
    reject buttons) is fired right after, so the operator sees this
    mini-app upload identically to one sent via the bot UI.
    """
    user, session = auth
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None or payment.user_id != user.id:
        raise HTTPException(status_code=404, detail="payment not found")
    if payment.provider != "card_to_card":
        raise HTTPException(status_code=400, detail="payment is not a card_to_card invoice")
    if payment.payment_status not in {"waiting_receipt", "waiting"}:
        raise HTTPException(
            status_code=400,
            detail=f"payment already processed (status={payment.payment_status})",
        )

    blob = await photo.read()
    if not blob:
        raise HTTPException(status_code=400, detail="empty file")
    if len(blob) > _MAX_RECEIPT_BYTES:
        raise HTTPException(status_code=413, detail="receipt too large (max 6 MB)")

    token = settings.bot_token.get_secret_value()
    if not token or token == "CHANGE_ME":
        raise HTTPException(status_code=500, detail="bot token not configured")

    # 1. Send the photo to the user's own DM (the bot can't message a
    #    user it hasn't seen — but the user just signed in, so the bot
    #    has them). This gives us a stable file_id.
    file_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            files = {"photo": (photo.filename or "receipt.jpg", BytesIO(blob), photo.content_type or "image/jpeg")}
            data = {"chat_id": str(user.telegram_id), "caption": "🧾 رسید پرداخت — ثبت‌شده از مینی‌اپ"}
            r = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data=data,
                files=files,
            )
            r.raise_for_status()
            result = (r.json() or {}).get("result") or {}
            photos = result.get("photo") or []
            if photos:
                # Use the LARGEST size — last in the array — for the canonical file_id.
                file_id = photos[-1].get("file_id")
    except Exception as exc:
        logger.error("sendPhoto failed for payment %s: %s", payment_id, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="failed to upload receipt to Telegram") from exc

    if not file_id:
        raise HTTPException(status_code=502, detail="Telegram did not return a file_id")

    payload = dict(payment.callback_payload or {})
    payload["receipt_file_id"] = file_id
    payload["receipt_source"] = "miniapp"
    payload["receipt_uploaded_at"] = datetime.now(timezone.utc).isoformat()
    payment.callback_payload = payload
    payment.provider_payment_id = file_id
    payment.payment_status = "pending_approval"
    await session.commit()

    # 2. Best-effort: also notify the admin chats so they see the receipt
    #    + approve/reject buttons immediately. Reuse the bot-side
    #    notification helper.
    try:
        from apps.bot.premium_bot import PremiumEmojiBot
        from aiogram.client.default import DefaultBotProperties
        # Forward the receipt to admins via the same helper used by
        # the bot's TopUpStates.waiting_for_card_receipt handler.
        bot = PremiumEmojiBot(
            token=token,
            default=DefaultBotProperties(parse_mode=settings.bot_parse_mode),
        )
        try:
            from aiogram.utils.keyboard import InlineKeyboardBuilder
            from sqlalchemy import select as _sel
            admin_ids: set[int] = set()
            if settings.owner_telegram_id:
                admin_ids.add(int(settings.owner_telegram_id))
            result = await session.execute(_sel(User.telegram_id).where(User.role.in_(["admin", "owner"])))
            admin_ids.update(int(x) for x in result.scalars().all())

            builder = InlineKeyboardBuilder()
            builder.button(text="تایید و شارژ کیف پول", callback_data=f"mp:ok:{payment.id}")
            builder.button(text="رد پرداخت", callback_data=f"mp:no:{payment.id}")
            builder.adjust(1)
            caption = (
                "درخواست شارژ کارت به کارت (از مینی‌اپ)\n\n"
                f"Telegram ID: <code>{user.telegram_id}</code>\n"
                f"مبلغ: <b>{int(payment.pay_amount or 0):,} تومان</b>\n"
                f"شارژ دلاری: <b>{payment.price_amount:.2f} USD</b>\n\n"
                "بعد از بررسی رسید، تایید یا رد کنید."
            )
            for admin_id in admin_ids:
                try:
                    await bot.send_photo(admin_id, photo=file_id, caption=caption, reply_markup=builder.as_markup())
                except Exception:
                    continue
        finally:
            await bot.session.close()
    except Exception as exc:
        logger.warning("admin notify after miniapp receipt upload failed: %s", exc)

    return {
        "ok": True,
        "message": "رسید شما با موفقیت ثبت شد. بعد از تأیید مدیر، کیف پول شارژ می‌شود.",
    }
