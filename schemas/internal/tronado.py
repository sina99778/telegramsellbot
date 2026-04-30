from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class TronadoConvertTomanRequest(BaseModel):
    Toman: int
    Wallet: str


class TronadoConvertTomanResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    TronAmount: Decimal
    TronSunAmount: Decimal | None = None


class TronadoCreateOrderRequest(BaseModel):
    PaymentID: str
    WalletAddress: str
    TronAmount: Decimal
    CallbackUrl: str
    wageFromBusinessPercentage: int = 0
    apiVersion: int = 1


class TronadoOrderData(BaseModel):
    model_config = ConfigDict(extra="allow")

    Token: str
    FullPaymentUrl: str
    ErrorMessage: str | None = None
    EstimatedTomanAmount: str | None = None
    EstimatedTomanAmountExpireDateUtc: str | None = None


class TronadoCreateOrderResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    IsSuccessful: bool = False
    Code: int | None = None
    Message: str | None = None
    Data: TronadoOrderData | None = None


class TronadoStatusRequest(BaseModel):
    Id: str


class TronadoStatusResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    UniqueCode: str | None = None
    PaymentID: str | None = None
    UserTelegramId: int | None = None
    Wallet: str | None = None
    Hash: str | None = None
    TronAmount: Decimal | None = None
    ActualTronAmount: Decimal | None = None
    OrderStatusID: int | None = None
    OrderStatusTitle: str | None = None
    IsPaid: bool = False
    PaymentDate: str | None = None
    Error: str | None = None


class TronadoCallbackPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    payment_id: str = Field(alias="PaymentID")
    user_telegram_id: int | None = Field(default=None, alias="UserTelegramId")
    wallet: str | None = Field(default=None, alias="Wallet")
    tron_amount: Decimal | None = Field(default=None, alias="TronAmount")
    actual_tron_amount: Decimal | None = Field(default=None, alias="ActualTronAmount")
    callback_url: str | None = Field(default=None, alias="CallbackUrl")
