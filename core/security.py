from __future__ import annotations

import hashlib

from cryptography.fernet import Fernet, InvalidToken

from core.config import settings


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


def secret_key_fingerprint() -> str:
    """A short, ONE-WAY fingerprint of APP_SECRET_KEY (safe to log / show).

    Lets the operator confirm two backups — or an old vs new server — use the
    SAME encryption key, without ever revealing the key itself. A mismatch means
    encrypted panel passwords from one won't decrypt under the other.
    """
    raw = settings.app_secret_key.get_secret_value().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def encrypt_secret(value: str) -> str:
    try:
        return _build_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
    except Exception as exc:  # pragma: no cover - library failures are rare
        raise EncryptionError("Failed to encrypt secret value.") from exc


def decrypt_secret(value: str) -> str:
    try:
        return _build_fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise EncryptionError("Invalid encrypted secret.") from exc


def _build_fernet() -> Fernet:
    return Fernet(settings.app_secret_key.get_secret_value().encode("utf-8"))
