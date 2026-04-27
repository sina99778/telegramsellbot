from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.plan import Plan


class PlanInventory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "plan_inventories"

    plan_id: Mapped[UUID] = mapped_column(
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    sales_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sold_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    plan: Mapped[Plan] = relationship("Plan")
