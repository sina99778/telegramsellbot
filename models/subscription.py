from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.order import Order
    from models.plan import Plan
    from models.user import User
    from models.xui import XUIClientRecord


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="pending_activation",
        server_default="pending_activation",
    )
    activation_mode: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default="first_use",
        server_default="first_use",
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    volume_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    sub_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_usage_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    order: Mapped[Order | None] = relationship("Order", back_populates="subscription")
    plan: Mapped[Plan | None] = relationship("Plan", back_populates="subscriptions")
    xui_client: Mapped[XUIClientRecord | None] = relationship(
        "XUIClientRecord",
        back_populates="subscription",
        uselist=False,
    )
