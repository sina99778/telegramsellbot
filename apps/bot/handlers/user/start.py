from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from apps.bot.keyboards.user import get_main_menu_keyboard
from repositories.user import UserRepository


router = Router(name="user-start")


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

    welcome_name = user.first_name or telegram_user.first_name or "there"

    if is_created:
        welcome_text = (
            f"Welcome, {welcome_name}.\n\n"
            "Your account and wallet are ready. From here you can buy configs, "
            "manage your balance, and access support."
        )
    else:
        welcome_text = (
            f"Welcome back, {welcome_name}.\n\n"
            "Your dashboard is ready. Use the menu below to continue."
        )

    await message.answer(
        welcome_text,
        reply_markup=get_main_menu_keyboard(),
    )
