"""
Disaster-recovery sanity checks run at bot startup.

The #1 way a server migration goes wrong: the DB is restored but APP_SECRET_KEY
is different (a fresh key, or the old .env wasn't carried over). Encrypted X-UI
panel passwords then decrypt to garbage — SILENTLY — and panel connections break
with confusing errors. This catches that LOUDLY on boot and tells the operator
exactly how to fix it, instead of letting it rot.
"""
from __future__ import annotations

import logging

from sqlalchemy import select

from core.database import AsyncSessionFactory
from core.security import EncryptionError, decrypt_secret, secret_key_fingerprint
from models.xui import XUIServerCredential

logger = logging.getLogger(__name__)


async def verify_encryption_key(bot) -> None:
    """Confirm APP_SECRET_KEY can decrypt the stored panel credentials. On a
    mismatch, alert every admin with a clear fix. Best-effort — never blocks boot."""
    try:
        async with AsyncSessionFactory() as session:
            cred = await session.scalar(
                select(XUIServerCredential)
                .where(XUIServerCredential.password_encrypted.isnot(None))
                .limit(1)
            )
            if cred is None:
                logger.info("DR check: no encrypted panel credentials yet — skipping key check")
                return

            try:
                decrypt_secret(cred.password_encrypted)
                logger.info(
                    "DR check: APP_SECRET_KEY OK — panel credentials decrypt cleanly (fp=%s)",
                    secret_key_fingerprint(),
                )
                return
            except EncryptionError:
                pass

            # The key does NOT match the data that's in the DB.
            logger.critical(
                "DR check: APP_SECRET_KEY MISMATCH — cannot decrypt panel passwords "
                "(current fp=%s). Restore the old key or panel access stays broken.",
                secret_key_fingerprint(),
            )
            try:
                from services.notifications import notify_admins
                await notify_admins(
                    session,
                    bot,
                    "🚨 <b>هشدارِ بحرانی: کلیدِ رمزنگاری با دیتابیس نمی‌خونه!</b>\n\n"
                    "ربات نمی‌تواند رمزِ پنل‌های X-UI را رمزگشایی کند — یعنی "
                    "<code>APP_SECRET_KEY</code> با این دیتابیس هماهنگ نیست "
                    "(معمولاً بعد از جابه‌جاییِ سرور یا تغییرِ کلید).\n\n"
                    "🔧 <b>راه‌حل:</b> مقدارِ <code>APP_SECRET_KEY</code> را از فایلِ "
                    "<code>.env</code>ِ سرورِ <b>قبلی</b> در <code>.env</code>ِ این سرور بگذار و "
                    "ربات را ری‌استارت کن.\n\n"
                    "⚠️ تا آن موقع، اتصال به پنل‌ها و provisioning کار نمی‌کند.",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("DR check: could not alert admins about key mismatch: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("DR check skipped: %s", exc)
