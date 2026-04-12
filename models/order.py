from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.plan import Plan
    from models.subscription import Subscription
    from models.user import User


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(ForeignKey("plans.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="paid")
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="bot")
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(16), nullable=False)

    user: Mapped[User] = relationship("User", back_populates="orders")
    plan: Mapped[Plan] = relationship("Plan", back_populates="orders")
    subscription: Mapped[Subscription | None] = relationship("Subscription", back_populates="order", uselist=False)
