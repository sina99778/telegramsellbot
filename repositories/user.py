from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.user import User
from models.wallet import Wallet
from repositories.base import AsyncRepository


class UserRepository(AsyncRepository[User]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session=session, model=User)

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        query = (
            select(User)
            .options(
                selectinload(User.wallet),
                selectinload(User.profile),
            )
            .where(User.telegram_id == telegram_id)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_or_create_user(
        self,
        *,
        telegram_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None = None,
        language_code: str | None = None,
    ) -> tuple[User, bool]:
        """
        Get the user by Telegram ID, or create both the user and wallet atomically.

        The nested transaction makes the operation safe under concurrent `/start`
        calls. If another request creates the same Telegram user first, we recover
        by re-reading the row instead of failing the flow.
        """
        existing_user = await self.get_by_telegram_id(telegram_id)
        if existing_user is not None:
            return existing_user, False

        try:
            async with self.session.begin_nested():
                user = User(
                    telegram_id=telegram_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    language_code=language_code,
                )
                self.session.add(user)
                await self.session.flush()

                wallet = Wallet(
                    user_id=user.id,
                    balance=Decimal("0"),
                    credit_limit=Decimal("0"),
                    hold_balance=Decimal("0"),
                )
                self.session.add(wallet)
                await self.session.flush()

            created_user = await self.get_by_telegram_id(telegram_id)
            if created_user is None:
                raise RuntimeError("User creation completed but the user could not be reloaded.")

            return created_user, True

        except IntegrityError:
            # Another concurrent worker likely created the same Telegram user.
            await self.session.rollback()

            concurrent_user = await self.get_by_telegram_id(telegram_id)
            if concurrent_user is None:
                raise

            return concurrent_user, False
