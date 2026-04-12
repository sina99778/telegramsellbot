from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from models.wallet import WalletTransaction
from repositories.wallet import WalletRepository


class WalletError(Exception):
    """Base wallet domain exception."""


class WalletNotFoundError(WalletError):
    """Raised when a wallet does not exist for the target user."""


class InsufficientBalanceError(WalletError):
    """Raised when a debit would exceed the wallet's allowed negative balance."""


@dataclass(slots=True, frozen=True)
class WalletTransactionResult:
    transaction: WalletTransaction
    balance_before: Decimal
    balance_after: Decimal


class WalletManager:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.wallet_repository = WalletRepository(session)

    async def process_transaction(
        self,
        *,
        user_id: UUID,
        amount: Decimal,
        transaction_type: str,
        direction: str,
        currency: str,
        reference_type: str | None = None,
        reference_id: UUID | None = None,
        description: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> WalletTransactionResult:
        """
        Atomically update a wallet balance and append a ledger transaction.

        Rules:
        - `amount` must be positive.
        - `direction` must be either `credit` or `debit`.
        - For debits, the final balance may not be less than `-credit_limit`.
        """
        normalized_amount = amount.quantize(Decimal("0.00000001"))
        normalized_direction = direction.lower().strip()

        if normalized_amount <= Decimal("0"):
            raise ValueError("Transaction amount must be greater than zero.")

        if normalized_direction not in {"credit", "debit"}:
            raise ValueError("Transaction direction must be either 'credit' or 'debit'.")

        async with self.session.begin_nested():
            wallet = await self.wallet_repository.get_by_user_id_for_update(user_id)
            if wallet is None:
                raise WalletNotFoundError(f"Wallet not found for user_id={user_id}.")

            balance_before = wallet.balance
            balance_after = (
                balance_before + normalized_amount
                if normalized_direction == "credit"
                else balance_before - normalized_amount
            )

            minimum_allowed_balance = wallet.credit_limit * Decimal("-1")
            if normalized_direction == "debit" and balance_after < minimum_allowed_balance:
                raise InsufficientBalanceError(
                    "Wallet debit exceeds the allowed reseller credit limit."
                )

            wallet.balance = balance_after
            self.session.add(wallet)
            await self.session.flush()

            transaction = WalletTransaction(
                wallet_id=wallet.id,
                user_id=user_id,
                type=transaction_type,
                direction=normalized_direction,
                amount=normalized_amount,
                currency=currency,
                balance_before=balance_before,
                balance_after=balance_after,
                reference_type=reference_type,
                reference_id=reference_id,
                description=description,
                metadata_=metadata or {},
            )
            created_transaction = await self.wallet_repository.create_transaction(transaction)

        return WalletTransactionResult(
            transaction=created_transaction,
            balance_before=balance_before,
            balance_after=balance_after,
        )
