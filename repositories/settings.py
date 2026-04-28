from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.texts import MarketingTexts
from models.app_setting import AppSetting


RETARGETING_SETTINGS_KEY = "marketing.retargeting"

# Sentinel value to distinguish "not provided" from None (which clears a key)
_SENTINEL = object()


RENEWAL_SETTINGS_KEY = "service.renewal"
CUSTOM_PURCHASE_SETTINGS_KEY = "service.custom_purchase"
PHONE_VERIFICATION_SETTINGS_KEY = "user.phone_verification"

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

    # ── Payment gateway settings ─────────────────────────────────────────────

    GATEWAY_SETTINGS_KEY = "payment.gateways"

    @dataclass(slots=True)
    class GatewaySettings:
        nowpayments_enabled: bool
        tetrapay_enabled: bool
        nowpayments_api_key: str | None
        tetrapay_api_key: str | None
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
                nowpayments_api_key=None,
                tetrapay_api_key=None,
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
            nowpayments_api_key=payload.get("nowpayments_api_key"),
            tetrapay_api_key=payload.get("tetrapay_api_key"),
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
        nowpayments_api_key: str | None = _SENTINEL,
        tetrapay_api_key: str | None = _SENTINEL,
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
        if nowpayments_api_key is not _SENTINEL:
            payload["nowpayments_api_key"] = nowpayments_api_key
        if tetrapay_api_key is not _SENTINEL:
            payload["tetrapay_api_key"] = tetrapay_api_key
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
