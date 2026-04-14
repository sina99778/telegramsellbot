from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


if TYPE_CHECKING:
    from models.subscription import Subscription


class XUIServerRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "xui_servers"

    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    panel_type: Mapped[str] = mapped_column(String(32), nullable=False, default="sanaei_xui")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    health_status: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    # Port used by X-UI subscription service (different from admin panel port)
    # X-UI Sanaei default sub port is 2096 over HTTP
    subscription_port: Mapped[int] = mapped_column(Integer, nullable=False, default=2096, server_default="2096")
    # Domain used for generating VLESS/VMess config URIs (e.g. "proxy.example.com")
    config_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Domain used for subscription links (e.g. "sub.example.com")
    sub_domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Maximum number of active clients allowed to be provisioned on this server
    max_clients: Mapped[int | None] = mapped_column(Integer, nullable=True)

    inbounds: Mapped[list[XUIInboundRecord]] = relationship("XUIInboundRecord", back_populates="server")
    credentials: Mapped[XUIServerCredential | None] = relationship(
        "XUIServerCredential",
        back_populates="server",
        cascade="all, delete-orphan",
        uselist=False,
    )


class XUIInboundRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "xui_inbounds"

    server_id: Mapped[UUID] = mapped_column(ForeignKey("xui_servers.id", ondelete="CASCADE"), nullable=False)
    xui_inbound_remote_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
    protocol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reserved_for_reseller_id: Mapped[UUID | None] = mapped_column(nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        default=dict,
        server_default="{}",
    )

    server: Mapped[XUIServerRecord] = relationship("XUIServerRecord", back_populates="inbounds")
    clients: Mapped[list[XUIClientRecord]] = relationship("XUIClientRecord", back_populates="inbound")


class XUIClientRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "xui_clients"

    subscription_id: Mapped[UUID] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    inbound_id: Mapped[UUID] = mapped_column(ForeignKey("xui_inbounds.id", ondelete="CASCADE"), nullable=False)
    xui_client_remote_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    email: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    client_uuid: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    sub_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    subscription: Mapped[Subscription] = relationship("Subscription", back_populates="xui_client")
    inbound: Mapped[XUIInboundRecord] = relationship("XUIInboundRecord", back_populates="clients")


class XUIServerCredential(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "xui_server_credentials"

    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("xui_servers.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    username: Mapped[str] = mapped_column(Text, nullable=False)
    password_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    session_cookie_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)

    server: Mapped[XUIServerRecord] = relationship("XUIServerRecord", back_populates="credentials")
