from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.user import User


class BroadcastJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "broadcast_jobs"

    created_by_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    message_type: Mapped[str] = mapped_column(String(24), nullable=False, default="text")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    processed_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failed_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    created_by: Mapped[User] = relationship("User")
