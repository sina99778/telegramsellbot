from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from models.plan import Plan


class ReadyConfigPool(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ready_config_pools"

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    plan: Mapped[Plan] = relationship("Plan")
    items: Mapped[list[ReadyConfigItem]] = relationship(
        "ReadyConfigItem",
        back_populates="pool",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ReadyConfigItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ready_config_items"

    pool_id: Mapped[UUID] = mapped_column(ForeignKey("ready_config_pools.id", ondelete="CASCADE"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="available", server_default="available")
    assigned_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pool: Mapped[ReadyConfigPool] = relationship("ReadyConfigPool", back_populates="items")
