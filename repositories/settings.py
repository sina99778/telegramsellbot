from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.texts import MarketingTexts
from models.app_setting import AppSetting


RETARGETING_SETTINGS_KEY = "marketing.retargeting"


RENEWAL_SETTINGS_KEY = "service.renewal"

@dataclass(slots=True)
class RenewalSettings:
    price_per_gb: float
    price_per_10_days: float


@dataclass(slots=True)
class RetargetingSettings:
    enabled: bool
    days: int
    message: str


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
