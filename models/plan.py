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

    # Per-plan IP limit on the X-UI client. NULL → use global
    # ServiceSecuritySettings.xui_limit_ip. 0 → unlimited (X-UI semantics).
    ip_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Per-plan renewal pricing — both NULL → fall back to the global
    # RenewalSettings (price_per_gb, price_per_10_days). Stored in the
    # plan's own currency.
    renewal_price_per_gb: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    renewal_price_per_day: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)

    inbound: Mapped[XUIInboundRecord | None] = relationship("XUIInboundRecord", foreign_keys=[inbound_id])
    orders: Mapped[list[Order]] = relationship("Order", back_populates="plan")
    subscriptions: Mapped[list[Subscription]] = relationship("Subscription", back_populates="plan")

    # ── Resolution helpers ────────────────────────────────────────────
    # Each `effective_*` method returns "what value should this plan
    # actually use", taking the corresponding global default as a
    # fallback when the plan-level field is unset.

    def effective_ip_limit(self, global_default: int) -> int:
        """`ip_limit` is None → use global. Note 0 means unlimited on X-UI."""
        if self.ip_limit is None:
            return int(global_default)
        return int(self.ip_limit)

    def effective_renewal_price_per_gb(self, global_per_gb: float) -> float:
        if self.renewal_price_per_gb is None:
            return float(global_per_gb)
        return float(self.renewal_price_per_gb)

    def effective_renewal_price_per_day(self, global_per_10_days: float) -> float:
        """The global setting is per-10-days; the plan override is per-day."""
        if self.renewal_price_per_day is None:
            return float(global_per_10_days) / 10.0
        return float(self.renewal_price_per_day)
