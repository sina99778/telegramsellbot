from __future__ import annotations

import base64
import logging

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


_PLACEHOLDER_VALUES = {"CHANGE_ME", "CHANGE_ME_BASE64_32BYTE_FERNET_KEY", ""}


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    return value.strip() in _PLACEHOLDER_VALUES


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_debug: bool = False
    log_level: str = "INFO"
    app_secret_key: SecretStr = SecretStr("CHANGE_ME_BASE64_32BYTE_FERNET_KEY")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/telegramsellbot"
    database_echo: bool = False
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout: int = 60
    database_pool_recycle: int = 1800
    database_connect_timeout: int = 10

    redis_url: str = "redis://localhost:6379/0"

    bot_token: SecretStr = SecretStr("CHANGE_ME")
    bot_username: str | None = None
    bot_parse_mode: str = "HTML"
    bot_drop_pending_updates: bool = False
    premium_emoji_enabled: bool = False
    premium_emoji_map: dict[str, str] = {}

    xui_base_url: str = "http://127.0.0.1:2053/"
    xui_username: str = "admin"
    xui_password: SecretStr = SecretStr("CHANGE_ME")
    xui_verify_ssl: bool = True

    nowpayments_api_key: SecretStr = SecretStr("CHANGE_ME")
    nowpayments_base_url: str = "https://api.nowpayments.io/v1"
    nowpayments_ipn_secret: SecretStr | None = None

    web_base_url: str = "http://localhost:8000"
    nowpayments_ipn_callback_url: str = "http://localhost:8000/api/webhooks/nowpayments"

    tetrapay_api_key: SecretStr = SecretStr("CHANGE_ME")
    tetrapay_base_url: str = "https://tetra98.com/api"
    tetrapay_callback_url: str = "http://localhost:8000/api/webhooks/tetrapay"
    tetrapay_max_amount_toman: int = 5_000_000

    tronado_api_key: SecretStr = SecretStr("CHANGE_ME")
    tronado_base_url: str = "https://bot.tronado.cloud"
    tronado_callback_url: str = "http://localhost:8000/api/webhooks/tronado"
    tronado_wallet_address: str | None = None
    tronado_wage_from_business_percentage: int = 0

    # Blockchain-explorer API keys used by the crypto auto-confirm worker.
    # Both are OPTIONAL: leave unset for anonymous polling (works for
    # low volume), set them for higher request budgets on busy servers.
    #   TronGrid:  https://www.trongrid.io/ (free 100k req/day)
    #   TonCenter: https://toncenter.com/ (free 1 req/s without a key)
    trongrid_api_key:  SecretStr | None = None
    toncenter_api_key: SecretStr | None = None

    support_url: str | None = None
    owner_telegram_id: int | None = None
    admin_api_key: SecretStr | None = None

    # Compatibility flags ----------------------------------------------------
    # Allow /api/sub/{id} without a `?sig=` signature for subscriptions
    # created BEFORE the sig was rolled out. Now defaults to False (strict):
    # the provisioning path always mints SIGNED sub_links, so legitimately
    # issued configs are unaffected. Only pre-signature ready-config links
    # (if any were sold before the sig feature) are rejected — re-issue those
    # via the bot's "change link" action. Set SUB_LEGACY_UNSIGNED_ACCESS=true
    # in .env to temporarily re-open the grace window if needed.
    sub_legacy_unsigned_access: bool = False
    # Same idea for TetraPay invoices: existing pending invoices were
    # created with callback URLs that don't carry a `?t=` signature.
    # Keep True for the first deploy to drain in-flight invoices, then
    # set False to enforce HMAC strictly.
    tetrapay_legacy_unsigned_callback: bool = True

    # NOTE: the list of inbounds users can migrate TO via the
    # "🛠 تغییر سرور" flow used to live here as an .env list. It now lives
    # in the AppSettings DB table under "service.migration_targets" and
    # is managed through the admin bot UI:
    #   🖥 مدیریت سرورها → ⚙️ اینباندهای fallback

    @model_validator(mode="after")
    def _validate_secrets(self) -> "Settings":
        """Validate APP_SECRET_KEY and other secrets.

        Validation runs as a model-level (post-init) check so we can read
        ``app_env`` and stay lax in dev/test while failing fast in
        production. Fernet requires 32 url-safe-base64 bytes; placeholders
        slipped through silently before and caused lazy crashes the first
        time encrypt/decrypt was called.
        """
        is_prod = self.app_env.lower() in {"production", "prod"}
        raw = self.app_secret_key.get_secret_value()

        if _is_placeholder(raw):
            if is_prod:
                raise ValueError(
                    "APP_SECRET_KEY is not set. Generate one with "
                    "`python -c \"from cryptography.fernet import Fernet; "
                    "print(Fernet.generate_key().decode())\"` and put it in .env."
                )
            # Dev/test: leave placeholder in place so the bot can still
            # boot. Code that actually tries to encrypt/decrypt will fail
            # at the call-site instead of at startup.
        else:
            # If the user did supply a value, it must be a valid Fernet key.
            try:
                decoded = base64.urlsafe_b64decode(raw.encode("utf-8"))
            except Exception as exc:
                raise ValueError(
                    "APP_SECRET_KEY is not valid base64. Use Fernet.generate_key()."
                ) from exc
            if len(decoded) != 32:
                raise ValueError(
                    "APP_SECRET_KEY must decode to exactly 32 bytes (Fernet key)."
                )

        if is_prod:
            if _is_placeholder(self.bot_token.get_secret_value()):
                raise ValueError("BOT_TOKEN is required in production.")
            if self.owner_telegram_id is None:
                raise ValueError("OWNER_TELEGRAM_ID is required in production.")
        return self


settings = Settings()
