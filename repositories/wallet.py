from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from models.wallet import Wallet, WalletTransaction
from repositories.base import AsyncRepository


class WalletRepository(AsyncRepository[Wallet]):
    def __init__(self, session: AsyncSession) -> None:
        super().__init__(session=session, model=Wallet)

    async def get_by_user_id(self, user_id: UUID) -> Wallet | None:
        query = (
            select(Wallet)
            .options(selectinload(Wallet.transactions))
            .where(Wallet.user_id == user_id)
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def get_by_user_id_for_update(self, user_id: UUID) -> Wallet | None:
        """
        Lock the wallet row to prevent concurrent balance races.
        """
        query = (
            select(Wallet)
            .where(Wallet.user_id == user_id)
            .with_for_update()
        )
        result = await self.session.execute(query)
        return result.scalar_one_or_none()

    async def create_transaction(self, transaction: WalletTransaction) -> WalletTransaction:
        self.session.add(transaction)
        await self.session.flush()
        await self.session.refresh(transaction)
        return transaction
