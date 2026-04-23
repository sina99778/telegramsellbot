from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.user import get_main_menu_keyboard
from core.texts import Messages
from repositories.user import UserRepository

logger = logging.getLogger(__name__)

router = Router(name="user-start")


@router.message(CommandStart(deep_link=True))
async def start_deep_link_handler(
    message: Message,
    command: CommandObject,
    session: AsyncSession,
) -> None:
    """Handle /start with deep link (e.g., /start ref_ABC123)."""
    if message.from_user is None:
        return

    telegram_user = message.from_user
    user_repository = UserRepository(session)

    # Parse referral code from deep link
    referral_code: str | None = None
    if command.args and command.args.startswith("ref_"):
        referral_code = command.args[4:]  # Strip "ref_" prefix

    user, is_created = await user_repository.get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
        last_name=telegram_user.last_name,
        language_code=telegram_user.language_code,
    )

    welcome_name = user.first_name or telegram_user.first_name or "دوست عزیز"

    # Process referral only for newly created users
    if is_created and referral_code:
        try:
            await _process_referral(session, user_repository, user, referral_code)
        except Exception as exc:
            logger.warning("Failed to process referral for user %s: %s", user.id, exc)

    if is_created:
        welcome_text = Messages.WELCOME_NEW.format(name=welcome_name)
    else:
        welcome_text = Messages.WELCOME_BACK.format(name=welcome_name)

    from core.config import settings
    is_admin = user.role in {"admin", "owner"} or telegram_user.id == settings.owner_telegram_id

    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(is_admin=is_admin),
    )


@router.message(CommandStart())
async def start_command_handler(message: Message, session: AsyncSession) -> None:
    """
    Onboard the Telegram user into the local database and ensure a wallet exists.
    """
    if message.from_user is None:
        return

    telegram_user = message.from_user

    user_repository = UserRepository(session)

    user, is_created = await user_repository.get_or_create_user(
        telegram_id=telegram_user.id,
        username=telegram_user.username,
        first_name=telegram_user.first_name,
        last_name=telegram_user.last_name,
        language_code=telegram_user.language_code,
    )

    welcome_name = user.first_name or telegram_user.first_name or "دوست عزیز"

    if is_created:
        welcome_text = Messages.WELCOME_NEW.format(name=welcome_name)
    else:
        welcome_text = Messages.WELCOME_BACK.format(name=welcome_name)

    from core.config import settings
    is_admin = user.role in {"admin", "owner"} or telegram_user.id == settings.owner_telegram_id

    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(is_admin=is_admin),
    )


async def _process_referral(
    session: AsyncSession,
    user_repository: UserRepository,
    new_user,
    referral_code: str,
) -> None:
    """Link a newly created user to the referrer via ref code."""
    from models.user import User
    from sqlalchemy import select

    # Find referrer by ref_code
    referrer = await session.scalar(
        select(User).where(User.ref_code == referral_code)
    )
    if referrer is None:
        logger.info("Referral code '%s' not found, skipping", referral_code)
        return

    # Don't allow self-referral
    if referrer.id == new_user.id:
        return

    # Set the referred_by relationship
    new_user.referred_by_user_id = referrer.id
    session.add(new_user)
    await session.flush()

    logger.info(
        "User %s (tg=%s) referred by %s (tg=%s) via code '%s'",
        new_user.id, new_user.telegram_id,
        referrer.id, referrer.telegram_id,
        referral_code,
    )
