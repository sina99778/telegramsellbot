from __future__ import annotations

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_pool_timeout: int = 30
    database_pool_recycle: int = 1800

    redis_url: str = "redis://localhost:6379/0"

    bot_token: SecretStr = SecretStr("CHANGE_ME")
    bot_username: str | None = None
    bot_parse_mode: str = "HTML"
    bot_drop_pending_updates: bool = False

    xui_base_url: str = "http://127.0.0.1:2053/"
    xui_username: str = "admin"
    xui_password: SecretStr = SecretStr("CHANGE_ME")

    nowpayments_api_key: SecretStr = SecretStr("CHANGE_ME")
    nowpayments_base_url: str = "https://api.nowpayments.io/v1"
    nowpayments_ipn_secret: SecretStr | None = None

    web_base_url: str = "http://localhost:8000"
    nowpayments_ipn_callback_url: str = "http://localhost:8000/api/webhooks/nowpayments"
    
    tetrapay_api_key: SecretStr = SecretStr("CHANGE_ME")
    tetrapay_base_url: str = "https://tetra98.com/api"
    tetrapay_callback_url: str = "http://localhost:8000/api/webhooks/tetrapay"
    tetrapay_max_amount_toman: int = 5_000_000  # Max per-transaction limit in Tomans
    
    support_url: str | None = None
    owner_telegram_id: int | None = None
    admin_api_key: SecretStr | None = None


settings = Settings()
