from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text
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
    # Consecutive health-check failures. The health job only flips a server to
    # "unhealthy" (and alerts) after several in a row, so one transient blip
    # doesn't stop sales. Reset to 0 on any successful check.
    health_check_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
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
    # Panel dispatch for non-X-UI panels. NULL/"xui" => this is an X-UI client
    # (the historical default — every existing row keeps its behaviour). For a
    # PasarGuard config this is "pasarguard" and `panel_username` holds the
    # canonical username used by the panel's /api/user/{username} endpoints
    # (we still mirror it into `email`/`username` so the generic config views
    # keep working). Both nullable so scripts/...999_auto_sync_columns adds
    # them on deploy with no hand-written migration.
    panel_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    panel_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

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


class OrphanedPanelClient(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A panel-side client/user that MAY exist remotely with no local owner.

    Written whenever a compensating delete after an UNCERTAIN panel write
    fails. At that moment the enclosing DB transaction is about to roll back,
    so xui_clients keeps no trace of the identity we just sent to the panel —
    without this row the only record would be a log line, and a client that
    the panel *did* accept would keep serving traffic at full quota forever
    while the user's retry mints a brand-new identity.

    Rows are therefore written by an autonomous transaction (independent of
    the failing one) and swept by the reconcile-orphans worker job, which
    retries the delete until the panel confirms the client is gone.
    ``resolved_at`` is NULL for exactly the rows that still need attention.
    """

    __tablename__ = "orphaned_panel_clients"

    server_id: Mapped[UUID] = mapped_column(
        ForeignKey("xui_servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # "xui" | "pasarguard" | "rebecca" — mirrors XUIClientRecord.panel_kind so
    # the sweep knows which delete primitive to call.
    panel_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="xui")
    # X-UI needs the inbound to address a client; the Marzban family does not.
    inbound_remote_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The X-UI client uuid, or the PasarGuard/Rebecca username.
    remote_client_id: Mapped[str] = mapped_column(String(128), nullable=False)
    # Purely diagnostic: lets an admin find the client in the panel UI.
    email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Which cleanup site produced the row (provisioning, migration, …).
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )
    # "deleted" (we removed it) | "already_gone" (panel had no such client)
    resolution: Mapped[str | None] = mapped_column(String(24), nullable=True)
