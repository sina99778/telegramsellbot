from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from core.config import settings


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


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
