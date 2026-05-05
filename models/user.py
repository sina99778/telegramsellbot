from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.order import Order
    from models.payment import Payment
    from models.subscription import Subscription
    from models.wallet import Wallet, WalletTransaction


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_bot_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    has_received_free_trial: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
    )
    referred_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    ref_code: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    role: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="user",
        server_default="user",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default="active",
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    personal_discount_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    referred_by: Mapped[User | None] = relationship(
        "User",
        remote_side="User.id",
        back_populates="referrals",
        foreign_keys=[referred_by_user_id],
    )
    referrals: Mapped[list[User]] = relationship(
        "User",
        back_populates="referred_by",
        foreign_keys="User.referred_by_user_id",
    )
    profile: Mapped[UserProfile | None] = relationship(
        "UserProfile",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    wallet: Mapped[Wallet | None] = relationship(
        "Wallet",
        back_populates="user",
        cascade="all, delete-orphan",
        uselist=False,
    )
    wallet_transactions: Mapped[list[WalletTransaction]] = relationship(
        "WalletTransaction",
        back_populates="user",
    )
    orders: Mapped[list[Order]] = relationship("Order", back_populates="user")
    payments: Mapped[list[Payment]] = relationship("Payment", back_populates="user")
    subscriptions: Mapped[list[Subscription]] = relationship("Subscription", back_populates="user")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    default_currency: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="USD",
        server_default="USD",
    )
    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship("User", back_populates="profile", uselist=False)
