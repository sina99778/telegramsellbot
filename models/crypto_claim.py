"""A chain transaction may fund only one payment, even after payment deletion."""
from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, TimestampMixin


class CryptoTransactionClaim(TimestampMixin, Base):
    __tablename__ = "crypto_transaction_claims"

    chain: Mapped[str] = mapped_column(String(32), primary_key=True)
    tx_hash: Mapped[str] = mapped_column(String(200), primary_key=True)
    payment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payments.id", ondelete="SET NULL"), nullable=True, index=True,
    )
