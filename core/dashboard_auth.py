"""
Dashboard authentication helpers — stdlib-only password hashing +
HMAC-signed session tokens.

Two reasons we don't pull in passlib/PyJWT here:

  1. Adds a dep and a wheel-build step to a small concern. Python's
     `hashlib.scrypt` and `hmac` cover the whole thing.

  2. The dashboard is a single-tenant tool. We don't need the
     scalability features of a full JWT library; we need
     "is-this-cookie-real-and-not-expired" which is 30 lines.

Format conventions:

  password_hash:   "scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>"
  session token:   "<payload_b64>.<sig_b64>"
                    payload = "<admin_uuid>.<expires_at>.<issued_at>.<session_version>"
                    sig     = HMAC-SHA256(payload, APP_SECRET_KEY)

The session cookie is HTTP-Only + Secure + SameSite=Lax.

Two independent clocks guard it:

  * `expires_at` is the *idle* window (14 days). It slides forward on
    every authenticated request, so an active operator never gets
    logged out mid-session.

  * `issued_at` is the *absolute* anchor (30 days). It is inherited
    verbatim by every re-issued token, which is the whole point: a
    sliding window with no ceiling means one stolen cookie grants
    permanent access as long as it keeps being used.

`session_version` mirrors `dashboard_admins.session_version` and is
compared on every request. Bumping that column is what makes logout,
password rotation and account disablement actually revoke outstanding
cookies — the only revocation lever a stateless signed token can have.

Tokens minted by the older 2-field format are rejected rather than
grandfathered in: they carry neither an anchor nor an epoch, so honouring
them would silently reopen the hole. Cost is one forced re-login on
deploy.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from core.config import settings


# ── Password hashing ─────────────────────────────────────────────────────
#
# scrypt parameters tuned for ~50ms verification on a 4-vCPU VPS.
# N=2**14, r=8, p=1 is the same baseline that argon2id RFC suggests
# for "interactive logins". MEM ≈ 16 MB which is fine on any prod host.
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_SALT_BYTES = 16


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def hash_password(password: str) -> str:
    """Return a portable scrypt hash string for the given plain password."""
    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    salt = os.urandom(_SCRYPT_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time check of a plain password against a stored hash.

    Returns False on any parsing error — never raises — so a malformed
    DB row can't take down login.
    """
    try:
        algo, n_s, r_s, p_s, salt_b64, digest_b64 = stored_hash.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n_s), int(r_s), int(p_s)
        salt = _b64d(salt_b64)
        expected = _b64d(digest_b64)
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt, n=n, r=r, p=p, dklen=len(expected),
        )
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


# ── Session tokens ───────────────────────────────────────────────────────

SESSION_COOKIE_NAME = "tsb_dashboard"
SESSION_TTL_SECONDS = 14 * 24 * 3600  # 14 days — idle window, slides forward

# Hard ceiling measured from the ORIGINAL login, never extended. Past this
# point the operator re-authenticates no matter how active they were. This
# is what bounds the damage of a leaked cookie; without it the sliding
# window above renews forever.
SESSION_ABSOLUTE_MAX_SECONDS = 30 * 24 * 3600  # 30 days

# Cookie scope on the public host. SameSite=Lax permits navigation from
# external links (e.g. Telegram bot link to /dashboard/login) while still
# blocking cross-origin XHR CSRF for the /api/dashboard/* endpoints.
SESSION_COOKIE_OPTS = dict(
    httponly=True,
    samesite="lax",
    secure=True,
    path="/",
)


def _secret_bytes() -> bytes:
    raw = settings.app_secret_key.get_secret_value()
    return raw.encode("utf-8") if isinstance(raw, str) else raw


@dataclass(slots=True, frozen=True)
class SessionPayload:
    admin_id: UUID
    expires_at: int  # unix seconds — end of the sliding idle window
    issued_at: int  # unix seconds — original login, anchors the hard ceiling
    session_version: int  # auth epoch this token was minted against


def issue_session(
    admin_id: UUID,
    ttl: int = SESSION_TTL_SECONDS,
    *,
    session_version: int = 0,
    issued_at: int | None = None,
) -> str:
    """Return a signed session token for the given admin uuid.

    `issued_at` must be passed through unchanged when *re-issuing* an
    existing session, so the absolute ceiling keeps counting from the
    original login. Omit it only when minting a brand-new session (login).
    """
    now = int(time.time())
    anchor = now if issued_at is None else int(issued_at)
    exp = now + int(ttl)
    payload = f"{admin_id}.{exp}.{anchor}.{int(session_version)}".encode("ascii")
    sig = hmac.new(_secret_bytes(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def verify_session(token: str) -> SessionPayload | None:
    """Validate a session token. Returns the payload or None.

    Constant-time signature check. Time-based rejection AFTER signature
    verification so an attacker can't probe expiry via timing.

    The 4-field split is unambiguous: a UUID contains hyphens but never
    dots, and the three trailing fields are plain integers. Anything with a
    different shape — including legacy 2-field tokens — is rejected.
    """
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
        expected_sig = hmac.new(_secret_bytes(), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        parts = payload.decode("ascii").split(".")
        if len(parts) != 4:
            return None
        admin_id = UUID(parts[0])
        exp = int(parts[1])
        anchor = int(parts[2])
        version = int(parts[3])
        now = int(time.time())
        if exp < now:
            return None
        # Absolute ceiling. Checked here rather than in the dependency so
        # every consumer of verify_session() inherits it for free.
        if now - anchor > SESSION_ABSOLUTE_MAX_SECONDS:
            return None
        return SessionPayload(
            admin_id=admin_id,
            expires_at=exp,
            issued_at=anchor,
            session_version=version,
        )
    except Exception:
        return None


def remaining_session_seconds(payload: SessionPayload, *, now: int | None = None) -> int:
    """TTL to use when re-issuing `payload`, clamped by the hard ceiling.

    Returns the normal idle window, except near the end of a session's life
    where it shrinks to whatever is left before the absolute deadline. Both
    the token TTL and the cookie `max_age` are driven from this so the
    browser never holds a cookie that outlives the token inside it.
    """
    current = int(time.time()) if now is None else int(now)
    deadline = int(payload.issued_at) + SESSION_ABSOLUTE_MAX_SECONDS
    return max(0, min(SESSION_TTL_SECONDS, deadline - current))


# ── CSRF token for state-mutating GETs (export endpoints etc) ────────────
#
# Not strictly needed for fetch-based JSON POST/PATCH from the SPA because
# we use SameSite=Lax + custom JSON content-type, both of which kill
# classic CSRF. Kept here for explicit use on any GET that triggers a
# side effect (e.g. CSV download with `action=cleanup`).

def issue_csrf(admin_id: UUID, ttl: int = 3600, *, session_version: int = 0) -> str:
    # Short-lived, so the anchor is always "now" — a CSRF token is never
    # re-issued in place the way a session cookie is.
    return issue_session(admin_id, ttl=ttl, session_version=session_version)


def verify_csrf(token: str, admin_id: UUID) -> bool:
    sess = verify_session(token)
    return sess is not None and sess.admin_id == admin_id


# ── Convenience: bcrypt-style "needs_rehash" check, future-proof ─────────

def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash uses parameters weaker than current defaults."""
    try:
        algo, n_s, r_s, p_s, *_ = stored_hash.split("$")
        if algo != "scrypt":
            return True
        if (int(n_s), int(r_s), int(p_s)) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return True
    except Exception:
        return True
    return False


# Convenience export used by login/me/logout endpoints.
def generate_strong_password(length: int = 16) -> str:
    """For the install.sh bootstrap path when operator wants auto-gen."""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))
