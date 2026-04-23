from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.user import User


class Payment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "payments"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False, default="nowpayments")
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="wallet_topup")
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    provider_invoice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_status: Mapped[str] = mapped_column(String(24), nullable=False, default="waiting")
    pay_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    price_currency: Mapped[str] = mapped_column(String(16), nullable=False)
    pay_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    pay_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    price_amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    actually_paid: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    invoice_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    user: Mapped[User] = relationship("User", back_populates="payments")
