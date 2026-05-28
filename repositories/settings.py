from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from core.texts import MarketingTexts
from models.app_setting import AppSetting


def _normalize_emoji_map(raw_map: Any) -> dict[str, str]:
    if not isinstance(raw_map, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw_map.items():
        clean_key = str(key or "").strip()
        clean_value = str(value or "").strip()
        if clean_key and clean_value:
            normalized[clean_key] = clean_value
    return normalized


RETARGETING_SETTINGS_KEY = "marketing.retargeting"

# Sentinel value to distinguish "not provided" from None (which clears a key)
_SENTINEL = object()


RENEWAL_SETTINGS_KEY = "service.renewal"
CUSTOM_PURCHASE_SETTINGS_KEY = "service.custom_purchase"
PHONE_VERIFICATION_SETTINGS_KEY = "user.phone_verification"
SERVICE_SECURITY_SETTINGS_KEY = "service.security"
PREMIUM_EMOJI_SETTINGS_KEY = "bot.premium_emoji"
USER_ACTIONS_SETTINGS_KEY = "user.actions"
BUTTON_STYLE_SETTINGS_KEY = "bot.button_style"

@dataclass(slots=True)
class RenewalSettings:
    price_per_gb: float
    price_per_10_days: float


@dataclass(slots=True)
class CustomPurchaseSettings:
    enabled: bool
    price_per_gb: float
    price_per_day: float


@dataclass(slots=True)
class RetargetingSettings:
    enabled: bool
    days: int
    message: str


@dataclass(slots=True)
class TrialSettings:
    enabled: bool


@dataclass(slots=True)
class PhoneVerificationSettings:
    enabled: bool
    mode: str


@dataclass(slots=True)
class ServiceSecuritySettings:
    xui_limit_ip: int
    max_distinct_ips: int
    auto_disable_ip_abuse: bool


@dataclass(slots=True)
class PremiumEmojiSettings:
    enabled: bool
    emoji_map: dict[str, str]


@dataclass(slots=True)
class UserActionsSettings:
    delete_enabled: bool
    refund_enabled: bool
    transfer_enabled: bool
    sales_enabled: bool = True
    renewals_enabled: bool = True


@dataclass(slots=True)
class ButtonStyleSettings:
    """Bot API 9.4 added a `style` field on InlineKeyboardButton.
    We expose it as a tiny role-based theme: the bot's keyboards tag
    each button with a semantic role (confirm / destructive / navigation /
    info), and the operator picks which Telegram color each role uses.

    Each role value must be one of "primary" (blue), "success" (green),
    "danger" (red), or "" (no style — use Telegram's default look).
    """
    enabled: bool
    confirm: str       # default: "success"
    destructive: str   # default: "danger"
    navigation: str    # default: "primary"
    info: str          # default: "primary"


REVENUE_SETTINGS_KEY = "admin.revenue_reset"

class AppSettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_revenue_reset_at(self) -> datetime | None:
        record = await self.session.get(AppSetting, REVENUE_SETTINGS_KEY)
        if record is None or not record.value_json:
            return None
        
        reset_at_str = record.value_json.get("reset_at")
        if not reset_at_str:
            return None
        
        try:
            return datetime.fromisoformat(reset_at_str)
        except ValueError:
            return None

    async def reset_revenue(self) -> None:
        record = await self.session.get(AppSetting, REVENUE_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=REVENUE_SETTINGS_KEY)
        
        record.value_json = {"reset_at": datetime.now(timezone.utc).isoformat()}
        self.session.add(record)
        await self.session.flush()

    async def get_renewal_settings(self) -> RenewalSettings:
        record = await self._get_or_create_renewal_record()
        payload = dict(record.value_json or {})
        return RenewalSettings(
            price_per_gb=float(payload.get("price_per_gb", 0.1)),
            price_per_10_days=float(payload.get("price_per_10_days", 0.1)),
        )

    async def update_renewal_settings(
        self,
        *,
        price_per_gb: float | None = None,
        price_per_10_days: float | None = None,
    ) -> RenewalSettings:
        record = await self._get_or_create_renewal_record()
        payload = dict(record.value_json or {})

        if price_per_gb is not None:
            payload["price_per_gb"] = price_per_gb
        if price_per_10_days is not None:
            payload["price_per_10_days"] = price_per_10_days

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return await self.get_renewal_settings()

    async def _get_or_create_renewal_record(self) -> AppSetting:
        record = await self.session.get(AppSetting, RENEWAL_SETTINGS_KEY)
        if record is not None:
            return record

        record = AppSetting(
            key=RENEWAL_SETTINGS_KEY,
            value_json={"price_per_gb": 0.1, "price_per_10_days": 0.1},
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_custom_purchase_settings(self) -> CustomPurchaseSettings:
        record = await self.session.get(AppSetting, CUSTOM_PURCHASE_SETTINGS_KEY)
        if record is None or not record.value_json:
            return CustomPurchaseSettings(enabled=False, price_per_gb=0.1, price_per_day=0.1)
        payload = dict(record.value_json or {})
        return CustomPurchaseSettings(
            enabled=bool(payload.get("enabled", False)),
            price_per_gb=float(payload.get("price_per_gb", 0.1)),
            price_per_day=float(payload.get("price_per_day", 0.1)),
        )

    async def update_custom_purchase_settings(
        self,
        *,
        enabled: bool | None = None,
        price_per_gb: float | None = None,
        price_per_day: float | None = None,
    ) -> CustomPurchaseSettings:
        record = await self._get_or_create_custom_purchase_record()
        payload = dict(record.value_json or {})

        if enabled is not None:
            payload["enabled"] = enabled
        if price_per_gb is not None:
            payload["price_per_gb"] = price_per_gb
        if price_per_day is not None:
            payload["price_per_day"] = price_per_day

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return await self.get_custom_purchase_settings()

    async def get_service_security_settings(self) -> ServiceSecuritySettings:
        record = await self.session.get(AppSetting, SERVICE_SECURITY_SETTINGS_KEY)
        if record is None or not record.value_json:
            return ServiceSecuritySettings(
                xui_limit_ip=1,
                max_distinct_ips=3,
                auto_disable_ip_abuse=True,
            )
        payload = dict(record.value_json or {})
        return ServiceSecuritySettings(
            xui_limit_ip=max(int(payload.get("xui_limit_ip", 1)), 0),
            max_distinct_ips=max(int(payload.get("max_distinct_ips", 3)), 0),
            auto_disable_ip_abuse=bool(payload.get("auto_disable_ip_abuse", True)),
        )

    async def update_service_security_settings(
        self,
        *,
        xui_limit_ip: int | None = None,
        max_distinct_ips: int | None = None,
        auto_disable_ip_abuse: bool | None = None,
    ) -> ServiceSecuritySettings:
        record = await self.session.get(AppSetting, SERVICE_SECURITY_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=SERVICE_SECURITY_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})

        if xui_limit_ip is not None:
            payload["xui_limit_ip"] = max(int(xui_limit_ip), 0)
        if max_distinct_ips is not None:
            payload["max_distinct_ips"] = max(int(max_distinct_ips), 0)
        if auto_disable_ip_abuse is not None:
            payload["auto_disable_ip_abuse"] = auto_disable_ip_abuse

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_service_security_settings()

    async def get_premium_emoji_settings(self) -> PremiumEmojiSettings:
        record = await self.session.get(AppSetting, PREMIUM_EMOJI_SETTINGS_KEY)
        if record is None or not record.value_json:
            return PremiumEmojiSettings(enabled=False, emoji_map={})
        payload = dict(record.value_json or {})
        raw_map = payload.get("emoji_map") or {}
        return PremiumEmojiSettings(
            enabled=bool(payload.get("enabled", False)),
            emoji_map=_normalize_emoji_map(raw_map),
        )

    async def update_premium_emoji_settings(
        self,
        *,
        enabled: bool | None = None,
        emoji_map: dict[str, str] | None = None,
    ) -> PremiumEmojiSettings:
        record = await self.session.get(AppSetting, PREMIUM_EMOJI_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=PREMIUM_EMOJI_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})

        if enabled is not None:
            payload["enabled"] = enabled
        if emoji_map is not None:
            payload["emoji_map"] = _normalize_emoji_map(emoji_map)

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_premium_emoji_settings()

    async def _get_or_create_custom_purchase_record(self) -> AppSetting:
        record = await self.session.get(AppSetting, CUSTOM_PURCHASE_SETTINGS_KEY)
        if record is not None:
            return record

        record = AppSetting(
            key=CUSTOM_PURCHASE_SETTINGS_KEY,
            value_json={"enabled": False, "price_per_gb": 0.1, "price_per_day": 0.1},
        )
        self.session.add(record)
        await self.session.flush()
        return record

    TRIAL_SETTINGS_KEY = "service.trial"

    async def get_trial_settings(self) -> TrialSettings:
        record = await self.session.get(AppSetting, self.TRIAL_SETTINGS_KEY)
        if record is None or not record.value_json:
            return TrialSettings(enabled=True)
        payload = dict(record.value_json)
        return TrialSettings(enabled=bool(payload.get("enabled", True)))

    async def update_trial_settings(self, *, enabled: bool | None = None) -> TrialSettings:
        record = await self.session.get(AppSetting, self.TRIAL_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=self.TRIAL_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})
        if enabled is not None:
            payload["enabled"] = enabled
        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_trial_settings()

    async def get_retargeting_settings(self) -> RetargetingSettings:
        record = await self._get_or_create_retargeting_record()
        payload = dict(record.value_json or {})
        message = str(payload.get("message") or MarketingTexts.RETARGETING_REMINDER)

        return RetargetingSettings(
            enabled=bool(payload.get("enabled", True)),
            days=max(int(payload.get("days", 30)), 1),
            message=message,
        )

    async def update_retargeting_settings(
        self,
        *,
        enabled: bool | None = None,
        days: int | None = None,
        message: str | None = None,
    ) -> RetargetingSettings:
        record = await self._get_or_create_retargeting_record()
        payload = dict(record.value_json or {})

        if enabled is not None:
            payload["enabled"] = enabled
        if days is not None:
            payload["days"] = max(days, 1)
        if message is not None:
            payload["message"] = message.strip()

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return await self.get_retargeting_settings()

    async def _get_or_create_retargeting_record(self) -> AppSetting:
        record = await self.session.get(AppSetting, RETARGETING_SETTINGS_KEY)
        if record is not None:
            return record

        record = AppSetting(
            key=RETARGETING_SETTINGS_KEY,
            value_json={
                "enabled": True,
                "days": 30,
                "message": MarketingTexts.RETARGETING_REMINDER,
            },
        )
        self.session.add(record)
        await self.session.flush()
        return record

    # ── Migration target inbounds (user-side "🛠 تغییر سرور" picker) ─────────
    #
    # We store the list of inbound UUIDs that users can migrate ONTO as a
    # JSON array under a single AppSetting key. Empty / unset means "every
    # active inbound" — that's the fallback the user-side picker uses when
    # the admin hasn't configured a dedicated fallback yet.

    MIGRATION_TARGETS_KEY = "service.migration_targets"

    async def get_migration_target_inbound_ids(self) -> list[str]:
        record = await self.session.get(AppSetting, self.MIGRATION_TARGETS_KEY)
        if record is None or not record.value_json:
            return []
        raw = record.value_json.get("inbound_ids") or []
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if item]

    async def set_migration_target_inbound_ids(self, inbound_ids: list[str]) -> None:
        # Dedupe + drop empties without changing the order the admin picked.
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in inbound_ids:
            s = str(raw).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            cleaned.append(s)

        record = await self.session.get(AppSetting, self.MIGRATION_TARGETS_KEY)
        if record is None:
            record = AppSetting(key=self.MIGRATION_TARGETS_KEY, value_json={})
        record.value_json = {"inbound_ids": cleaned}
        self.session.add(record)
        await self.session.flush()

    async def toggle_migration_target_inbound(self, inbound_id: str) -> bool:
        """Flip the membership of `inbound_id` in the migration targets list.
        Returns True if it ended up enabled, False if disabled."""
        current = await self.get_migration_target_inbound_ids()
        if inbound_id in current:
            await self.set_migration_target_inbound_ids(
                [x for x in current if x != inbound_id]
            )
            return False
        await self.set_migration_target_inbound_ids(current + [inbound_id])
        return True

    # ── Sales-report channel ──────────────────────────────────────────────
    #
    # Optional chat (channel or supergroup) that every purchase / renewal /
    # topup notification is routed to. When unset, notifications fall back
    # to the legacy "DM every admin" behaviour. When set, admins stop
    # getting their personal DMs spammed and instead read sales activity
    # from one shared channel.

    SALES_CHANNEL_KEY = "notifications.sales_channel"

    async def get_sales_report_chat_id(self) -> int | None:
        record = await self.session.get(AppSetting, self.SALES_CHANNEL_KEY)
        if record is None or not record.value_json:
            return None
        raw = record.value_json.get("chat_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def set_sales_report_chat_id(self, chat_id: int | None, title: str | None = None) -> None:
        record = await self.session.get(AppSetting, self.SALES_CHANNEL_KEY)
        if chat_id is None:
            # Clear the setting entirely.
            if record is not None:
                record.value_json = {}
                self.session.add(record)
                await self.session.flush()
            return
        if record is None:
            record = AppSetting(key=self.SALES_CHANNEL_KEY, value_json={})
        record.value_json = {"chat_id": int(chat_id), "title": (title or "")[:128]}
        self.session.add(record)
        await self.session.flush()

    # ── Toman exchange rate ──────────────────────────────────────────────────

    USD_TOMAN_RATE_KEY = "finance.usd_toman_rate"

    async def get_toman_rate(self) -> int:
        """Return the USD → Toman rate. Default 100000."""
        record = await self.session.get(AppSetting, self.USD_TOMAN_RATE_KEY)
        if record and record.value_json:
            return int(record.value_json.get("rate", 100000))
        return 100000

    async def set_toman_rate(self, rate: int) -> None:
        record = await self.session.get(AppSetting, self.USD_TOMAN_RATE_KEY)
        if record is None:
            record = AppSetting(key=self.USD_TOMAN_RATE_KEY)
        record.value_json = {"rate": rate}
        self.session.add(record)
        await self.session.flush()

    # ── Display currency mode (USD vs IRT/Toman) ─────────────────────────────
    #
    # Drives every user-facing price string via `core.formatting.format_money`.
    # Internally we always store USD (Numeric 18,8); this setting only
    # affects what customers SEE and how their topup input is INTERPRETED.
    # Conversion uses `get_toman_rate()` above.
    #
    #   mode="USD"  (default): customers see "12.50 $" and type USD on topup.
    #   mode="IRT":            customers see "2,187,500 تومان" and type Toman.
    DISPLAY_CURRENCY_KEY = "ui.display_currency"

    async def get_display_currency(self) -> str:
        record = await self.session.get(AppSetting, self.DISPLAY_CURRENCY_KEY)
        if record and record.value_json:
            mode = str(record.value_json.get("mode", "USD")).upper()
            if mode in ("USD", "IRT"):
                return mode
        return "USD"

    async def set_display_currency(self, mode: str) -> None:
        if mode not in ("USD", "IRT"):
            raise ValueError("display currency must be 'USD' or 'IRT'")
        record = await self.session.get(AppSetting, self.DISPLAY_CURRENCY_KEY)
        if record is None:
            record = AppSetting(key=self.DISPLAY_CURRENCY_KEY)
        record.value_json = {"mode": mode}
        self.session.add(record)
        await self.session.flush()

    # ── Backup schedule (interval + dedicated channel) ───────────────────────
    #
    # The scheduled-backup worker job fires every 30 minutes (cheap) and
    # ASKS this repo whether enough time has elapsed since the last run.
    # That way the operator can change the interval at any time from the
    # dashboard / bot and the next cycle picks it up — no worker restart.
    #
    # Destinations (in priority order):
    #   1. system.backup_channel_id     — dedicated backup channel
    #   2. notifications.sales_channel  — fall back to sales channel
    #   3. admin DMs                    — last-resort default
    BACKUP_INTERVAL_KEY    = "system.backup_interval_hours"
    BACKUP_CHANNEL_KEY     = "system.backup_channel_id"
    BACKUP_LAST_RUN_KEY    = "system.backup_last_run_at"

    async def get_backup_interval_hours(self) -> int:
        """Hours between auto-backups. Default 6. Min 1."""
        record = await self.session.get(AppSetting, self.BACKUP_INTERVAL_KEY)
        if record and record.value_json:
            try:
                hours = int(record.value_json.get("hours", 6))
                return max(1, hours)
            except (TypeError, ValueError):
                return 6
        return 6

    async def set_backup_interval_hours(self, hours: int) -> None:
        if hours < 1 or hours > 24 * 7:
            raise ValueError("backup_interval_hours must be between 1 and 168")
        record = await self.session.get(AppSetting, self.BACKUP_INTERVAL_KEY)
        if record is None:
            record = AppSetting(key=self.BACKUP_INTERVAL_KEY)
        record.value_json = {"hours": int(hours)}
        self.session.add(record)
        await self.session.flush()

    async def get_backup_channel_id(self) -> int | None:
        """Dedicated chat for backup file deliveries.

        Returns None if not configured — in that case the worker falls
        back to the sales-report channel, then to admin DMs.
        """
        record = await self.session.get(AppSetting, self.BACKUP_CHANNEL_KEY)
        if record is None or not record.value_json:
            return None
        raw = record.value_json.get("chat_id")
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def set_backup_channel_id(self, chat_id: int | None, title: str | None = None) -> None:
        record = await self.session.get(AppSetting, self.BACKUP_CHANNEL_KEY)
        if chat_id is None:
            if record is not None:
                record.value_json = {}
                self.session.add(record)
                await self.session.flush()
            return
        if record is None:
            record = AppSetting(key=self.BACKUP_CHANNEL_KEY)
        record.value_json = {"chat_id": int(chat_id), "title": (title or "")[:128]}
        self.session.add(record)
        await self.session.flush()

    async def get_backup_last_run_iso(self) -> str | None:
        record = await self.session.get(AppSetting, self.BACKUP_LAST_RUN_KEY)
        if record is None or not record.value_json:
            return None
        v = record.value_json.get("at")
        return str(v) if v else None

    async def set_backup_last_run_now(self) -> None:
        from datetime import datetime, timezone
        record = await self.session.get(AppSetting, self.BACKUP_LAST_RUN_KEY)
        if record is None:
            record = AppSetting(key=self.BACKUP_LAST_RUN_KEY)
        record.value_json = {"at": datetime.now(timezone.utc).isoformat()}
        self.session.add(record)
        await self.session.flush()

    # ── Payment gateway settings ─────────────────────────────────────────────

    GATEWAY_SETTINGS_KEY = "payment.gateways"

    @dataclass(slots=True)
    class GatewaySettings:
        nowpayments_enabled: bool
        tetrapay_enabled: bool
        tronado_enabled: bool
        nowpayments_api_key: str | None
        tetrapay_api_key: str | None
        tronado_api_key: str | None
        tronado_wallet_address: str | None
        tronado_wage_from_business_percentage: int | None
        nowpayments_ipn_secret: str | None
        manual_crypto_enabled: bool
        manual_crypto_currency: str | None  # e.g. "USDT TRC20"
        manual_crypto_address: str | None
        manual_crypto_wallets: list[dict[str, str]]
        card_to_card_enabled: bool
        card_number: str | None
        card_holder: str | None
        card_bank: str | None
        card_note: str | None
        force_join_channel: str | None  # e.g. "@mychannel" or "-1001234567890"
        force_join_enabled: bool

    async def get_gateway_settings(self) -> GatewaySettings:
        record = await self.session.get(AppSetting, self.GATEWAY_SETTINGS_KEY)
        if record is None or not record.value_json:
            return self.GatewaySettings(
                nowpayments_enabled=True,
                tetrapay_enabled=True,
                tronado_enabled=False,
                nowpayments_api_key=None,
                tetrapay_api_key=None,
                tronado_api_key=None,
                tronado_wallet_address=None,
                tronado_wage_from_business_percentage=None,
                nowpayments_ipn_secret=None,
                manual_crypto_enabled=False,
                manual_crypto_currency=None,
                manual_crypto_address=None,
                manual_crypto_wallets=[],
                card_to_card_enabled=False,
                card_number=None,
                card_holder=None,
                card_bank=None,
                card_note=None,
                force_join_channel=None,
                force_join_enabled=False,
            )
        payload = dict(record.value_json)
        manual_wallets = payload.get("manual_crypto_wallets") or []
        if not manual_wallets and payload.get("manual_crypto_address"):
            manual_wallets = [
                {
                    "currency": str(payload.get("manual_crypto_currency") or "Crypto"),
                    "address": str(payload.get("manual_crypto_address")),
                }
            ]
        return self.GatewaySettings(
            nowpayments_enabled=bool(payload.get("nowpayments_enabled", True)),
            tetrapay_enabled=bool(payload.get("tetrapay_enabled", True)),
            tronado_enabled=bool(payload.get("tronado_enabled", False)),
            nowpayments_api_key=payload.get("nowpayments_api_key"),
            tetrapay_api_key=payload.get("tetrapay_api_key"),
            tronado_api_key=payload.get("tronado_api_key"),
            tronado_wallet_address=payload.get("tronado_wallet_address"),
            tronado_wage_from_business_percentage=(
                int(payload["tronado_wage_from_business_percentage"])
                if payload.get("tronado_wage_from_business_percentage") is not None
                else None
            ),
            nowpayments_ipn_secret=payload.get("nowpayments_ipn_secret"),
            manual_crypto_enabled=bool(payload.get("manual_crypto_enabled", False)),
            manual_crypto_currency=payload.get("manual_crypto_currency"),
            manual_crypto_address=payload.get("manual_crypto_address"),
            manual_crypto_wallets=manual_wallets if isinstance(manual_wallets, list) else [],
            card_to_card_enabled=bool(payload.get("card_to_card_enabled", False)),
            card_number=payload.get("card_number"),
            card_holder=payload.get("card_holder"),
            card_bank=payload.get("card_bank"),
            card_note=payload.get("card_note"),
            force_join_channel=payload.get("force_join_channel"),
            force_join_enabled=bool(payload.get("force_join_enabled", False)),
        )

    async def update_gateway_settings(
        self,
        *,
        nowpayments_enabled: bool | None = None,
        tetrapay_enabled: bool | None = None,
        tronado_enabled: bool | None = None,
        nowpayments_api_key: str | None = _SENTINEL,
        tetrapay_api_key: str | None = _SENTINEL,
        tronado_api_key: str | None = _SENTINEL,
        tronado_wallet_address: str | None = _SENTINEL,
        tronado_wage_from_business_percentage: int | None = _SENTINEL,
        nowpayments_ipn_secret: str | None = _SENTINEL,
        manual_crypto_enabled: bool | None = None,
        manual_crypto_currency: str | None = _SENTINEL,
        manual_crypto_address: str | None = _SENTINEL,
        manual_crypto_wallets: list[dict[str, str]] | None = _SENTINEL,
        card_to_card_enabled: bool | None = None,
        card_number: str | None = _SENTINEL,
        card_holder: str | None = _SENTINEL,
        card_bank: str | None = _SENTINEL,
        card_note: str | None = _SENTINEL,
        force_join_channel: str | None = _SENTINEL,
        force_join_enabled: bool | None = None,
    ) -> "AppSettingsRepository.GatewaySettings":
        record = await self.session.get(AppSetting, self.GATEWAY_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=self.GATEWAY_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})

        if nowpayments_enabled is not None:
            payload["nowpayments_enabled"] = nowpayments_enabled
        if tetrapay_enabled is not None:
            payload["tetrapay_enabled"] = tetrapay_enabled
        if tronado_enabled is not None:
            payload["tronado_enabled"] = tronado_enabled
        if nowpayments_api_key is not _SENTINEL:
            payload["nowpayments_api_key"] = nowpayments_api_key
        if tetrapay_api_key is not _SENTINEL:
            payload["tetrapay_api_key"] = tetrapay_api_key
        if tronado_api_key is not _SENTINEL:
            payload["tronado_api_key"] = tronado_api_key
        if tronado_wallet_address is not _SENTINEL:
            payload["tronado_wallet_address"] = tronado_wallet_address
        if tronado_wage_from_business_percentage is not _SENTINEL:
            payload["tronado_wage_from_business_percentage"] = tronado_wage_from_business_percentage
        if nowpayments_ipn_secret is not _SENTINEL:
            payload["nowpayments_ipn_secret"] = nowpayments_ipn_secret
        if manual_crypto_enabled is not None:
            payload["manual_crypto_enabled"] = manual_crypto_enabled
        if manual_crypto_currency is not _SENTINEL:
            payload["manual_crypto_currency"] = manual_crypto_currency
        if manual_crypto_address is not _SENTINEL:
            payload["manual_crypto_address"] = manual_crypto_address
        if manual_crypto_wallets is not _SENTINEL:
            payload["manual_crypto_wallets"] = manual_crypto_wallets
        if card_to_card_enabled is not None:
            payload["card_to_card_enabled"] = card_to_card_enabled
        if card_number is not _SENTINEL:
            payload["card_number"] = card_number
        if card_holder is not _SENTINEL:
            payload["card_holder"] = card_holder
        if card_bank is not _SENTINEL:
            payload["card_bank"] = card_bank
        if card_note is not _SENTINEL:
            payload["card_note"] = card_note
        if force_join_channel is not _SENTINEL:
            payload["force_join_channel"] = force_join_channel
        if force_join_enabled is not None:
            payload["force_join_enabled"] = force_join_enabled

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_gateway_settings()

    # ── Referral settings ────────────────────────────────────────────────────

    async def get_phone_verification_settings(self) -> PhoneVerificationSettings:
        record = await self.session.get(AppSetting, PHONE_VERIFICATION_SETTINGS_KEY)
        if record is None or not record.value_json:
            return PhoneVerificationSettings(enabled=False, mode="iran")
        payload = dict(record.value_json)
        mode = str(payload.get("mode") or "iran").lower()
        if mode not in {"iran", "any"}:
            mode = "iran"
        return PhoneVerificationSettings(
            enabled=bool(payload.get("enabled", False)),
            mode=mode,
        )

    async def update_phone_verification_settings(
        self,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
    ) -> PhoneVerificationSettings:
        record = await self.session.get(AppSetting, PHONE_VERIFICATION_SETTINGS_KEY)
        if record is None:
            record = AppSetting(
                key=PHONE_VERIFICATION_SETTINGS_KEY,
                value_json={"enabled": False, "mode": "iran"},
            )
        payload = dict(record.value_json or {})

        if enabled is not None:
            payload["enabled"] = enabled
        if mode is not None:
            normalized_mode = mode.strip().lower()
            payload["mode"] = normalized_mode if normalized_mode in {"iran", "any"} else "iran"

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_phone_verification_settings()

    REFERRAL_SETTINGS_KEY = "referral.settings"

    @dataclass(slots=True)
    class ReferralSettings:
        enabled: bool
        referrer_bonus_usd: float
        referee_bonus_usd: float

    async def get_referral_settings(self) -> ReferralSettings:
        record = await self.session.get(AppSetting, self.REFERRAL_SETTINGS_KEY)
        if record is None or not record.value_json:
            return self.ReferralSettings(
                enabled=False,
                referrer_bonus_usd=0.5,
                referee_bonus_usd=0.0,
            )
        payload = dict(record.value_json)
        return self.ReferralSettings(
            enabled=bool(payload.get("enabled", False)),
            referrer_bonus_usd=float(payload.get("referrer_bonus_usd", 0.5)),
            referee_bonus_usd=float(payload.get("referee_bonus_usd", 0.0)),
        )

    async def update_referral_settings(
        self,
        *,
        enabled: bool | None = None,
        referrer_bonus_usd: float | None = None,
        referee_bonus_usd: float | None = None,
    ) -> "AppSettingsRepository.ReferralSettings":
        record = await self.session.get(AppSetting, self.REFERRAL_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=self.REFERRAL_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})

        if enabled is not None:
            payload["enabled"] = enabled
        if referrer_bonus_usd is not None:
            payload["referrer_bonus_usd"] = referrer_bonus_usd
        if referee_bonus_usd is not None:
            payload["referee_bonus_usd"] = referee_bonus_usd

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_referral_settings()


    # ── User actions (delete / refund toggles) ───────────────────────────

    async def get_user_actions_settings(self) -> 'UserActionsSettings':
        record = await self.session.get(AppSetting, USER_ACTIONS_SETTINGS_KEY)
        if record is None or not record.value_json:
            return UserActionsSettings(delete_enabled=True, refund_enabled=True, transfer_enabled=True, sales_enabled=True, renewals_enabled=True)
        payload = dict(record.value_json)
        return UserActionsSettings(
            delete_enabled=bool(payload.get('delete_enabled', True)),
            refund_enabled=bool(payload.get('refund_enabled', True)),
            transfer_enabled=bool(payload.get('transfer_enabled', True)),
            sales_enabled=bool(payload.get('sales_enabled', True)),
            renewals_enabled=bool(payload.get('renewals_enabled', True)),
        )

    async def update_user_actions_settings(
        self,
        *,
        delete_enabled: bool | None = None,
        refund_enabled: bool | None = None,
        transfer_enabled: bool | None = None,
        sales_enabled: bool | None = None,
        renewals_enabled: bool | None = None,
    ) -> 'UserActionsSettings':
        record = await self.session.get(AppSetting, USER_ACTIONS_SETTINGS_KEY)
        if record is None:
            record = AppSetting(
                key=USER_ACTIONS_SETTINGS_KEY,
                value_json={'delete_enabled': True, 'refund_enabled': True, 'transfer_enabled': True, 'sales_enabled': True, 'renewals_enabled': True},
            )
        payload = dict(record.value_json or {})

        if delete_enabled is not None:
            payload['delete_enabled'] = delete_enabled
        if refund_enabled is not None:
            payload['refund_enabled'] = refund_enabled
        if transfer_enabled is not None:
            payload['transfer_enabled'] = transfer_enabled
        if sales_enabled is not None:
            payload['sales_enabled'] = sales_enabled
        if renewals_enabled is not None:
            payload['renewals_enabled'] = renewals_enabled

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_user_actions_settings()

    # ── Button-style (Bot API 9.4) ───────────────────────────────────

    _VALID_BUTTON_STYLES = ("primary", "success", "danger", "")

    async def get_button_style_settings(self) -> 'ButtonStyleSettings':
        record = await self.session.get(AppSetting, BUTTON_STYLE_SETTINGS_KEY)
        defaults = dict(enabled=True, confirm="success", destructive="danger",
                        navigation="primary", info="primary")
        if record is None or not record.value_json:
            return ButtonStyleSettings(**defaults)
        payload = dict(record.value_json)
        def _v(k: str) -> str:
            v = str(payload.get(k, defaults[k]) or "").strip()
            return v if v in self._VALID_BUTTON_STYLES else defaults[k]
        return ButtonStyleSettings(
            enabled=bool(payload.get("enabled", True)),
            confirm=_v("confirm"),
            destructive=_v("destructive"),
            navigation=_v("navigation"),
            info=_v("info"),
        )

    async def update_button_style_settings(
        self,
        *,
        enabled: bool | None = None,
        confirm: str | None = None,
        destructive: str | None = None,
        navigation: str | None = None,
        info: str | None = None,
    ) -> 'ButtonStyleSettings':
        record = await self.session.get(AppSetting, BUTTON_STYLE_SETTINGS_KEY)
        if record is None:
            record = AppSetting(key=BUTTON_STYLE_SETTINGS_KEY, value_json={})
        payload = dict(record.value_json or {})

        def _validated(value: str | None) -> str | None:
            if value is None:
                return None
            v = value.strip()
            if v not in self._VALID_BUTTON_STYLES:
                raise ValueError(f"button style must be one of {self._VALID_BUTTON_STYLES}, got {value!r}")
            return v

        if enabled is not None:
            payload["enabled"] = bool(enabled)
        for field, value in (("confirm", confirm), ("destructive", destructive),
                             ("navigation", navigation), ("info", info)):
            v = _validated(value)
            if v is not None:
                payload[field] = v

        record.value_json = payload
        self.session.add(record)
        await self.session.flush()
        return await self.get_button_style_settings()
