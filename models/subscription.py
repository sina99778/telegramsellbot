from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.order import Order
    from models.plan import Plan
    from models.user import User
    from models.xui import XUIClientRecord


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        Index("ix_subscriptions_user_status", "user_id", "status"),
    )

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
    # Monotonic counter of bytes delivered BEFORE the most recent reset.
    # Every code path that resets `used_bytes` to 0 (volume renewal, inbound
    # migration, …) MUST first accumulate `used_bytes` into this column.
    # Resellers bill the operator on (lifetime_used_bytes + used_bytes) —
    # this column is what makes "total volume delivered" actually accurate
    # across renewals instead of dropping every cycle.
    lifetime_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    sub_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Origin marker. NULL for native subscriptions created via the bot's own
    # purchase flow. Set to "imported_legacy" by scripts/import_legacy.py for
    # rows brought in from the previous-generation MySQL bot, so the my_configs
    # display can render them differently (read-only viewer + "migrate to new
    # inbound" CTA) and provisioning.manager can skip the X-UI panel side
    # when the imported sub doesn't have an XUIClientRecord on our side.
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Optional human-friendly name carried over from the legacy bot.
    # We store it as a column (rather than digging through callback_payload)
    # because the migration flow needs to use it byte-for-byte as the new
    # X-UI client's remark — preserving names was the operator's hard
    # requirement ("سری قبل فاجعه به بار اومد").
    legacy_remark: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Raw VLESS / sub link the legacy bot served. Kept as Text because
    # full JSON arrays from old.orders_list.link can be long.
    legacy_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_usage_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship("User", back_populates="subscriptions")
    order: Mapped[Order | None] = relationship("Order", back_populates="subscription")
    plan: Mapped[Plan | None] = relationship("Plan", back_populates="subscriptions")
    xui_client: Mapped[XUIClientRecord | None] = relationship(
        "XUIClientRecord",
        back_populates="subscription",
        uselist=False,
    )
