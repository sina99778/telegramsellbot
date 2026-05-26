"""
DashboardAdmin — credentials for the web management dashboard.

Why a separate table?
---------------------
The User model is keyed on telegram_id and is fundamentally tied to a
Telegram account. The dashboard is a *desktop* tool — operators want
to log in from a browser with a username + password, not via Telegram.
Sharing the User table would mean tacking optional password fields on
every customer row, which is wasteful and leaks "is-a-dashboard-admin"
status into the customer model.

So we keep the two worlds separate. A telegram-side admin and a
dashboard-side admin can be the same human, but the credentials are
different objects.

Bootstrap
---------
Operators run `python scripts/dashboard_admin.py create` (wired into
install.sh menu) on first deploy to create their initial credentials.
The script is also re-runnable to add additional admins / rotate
passwords.

Hashing
-------
scrypt via `hashlib.scrypt` (stdlib — no new pip dep). N=2**14 keeps
verification ~50ms which is comfortable for an admin endpoint while
making brute-force prohibitively expensive.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DashboardAdmin(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "dashboard_admins"

    # Login identifier. Case-folded on write; index for fast lookup.
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True,
    )
    # scrypt(N=2**14) hash. Stored as "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>".
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # Optional human-readable label rendered in the top-bar avatar tooltip.
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Soft-disable without deleting (audit trail preserved).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # Stamped on every successful login for audit.
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
