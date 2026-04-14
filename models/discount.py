from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class DiscountCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "discount_codes"

    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    discount_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_uses: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional: limit to a specific plan
    plan_id: Mapped[UUID | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"), nullable=True)
