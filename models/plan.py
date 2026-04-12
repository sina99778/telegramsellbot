from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.order import Order
    from models.subscription import Subscription
    from models.xui import XUIInboundRecord


class Plan(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    inbound_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("xui_inbounds.id", ondelete="SET NULL"),
        nullable=True,
    )
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    volume_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    renewal_price: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False, default="USD")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    inbound: Mapped[XUIInboundRecord | None] = relationship("XUIInboundRecord", foreign_keys=[inbound_id])
    orders: Mapped[list[Order]] = relationship("Order", back_populates="plan")
    subscriptions: Mapped[list[Subscription]] = relationship("Subscription", back_populates="plan")
