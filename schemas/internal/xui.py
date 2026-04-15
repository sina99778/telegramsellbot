from __future__ import annotations

import json
from typing import Any, Generic, TypeVar

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


XUIResponseT = TypeVar("XUIResponseT")


class XUIAPIResponse(BaseModel, Generic[XUIResponseT]):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    success: bool = True
    msg: str | None = None
    obj: XUIResponseT | None = None


class XUILoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    username: str
    password: str


class XUILoginResponse(XUIAPIResponse[Any]):
    pass


class XUIClient(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    email: str
    id: str | None = Field(default=None, validation_alias=AliasChoices("id", "uuid"))
    uuid: str | None = None
    flow: str = ""
    limit_ip: int = Field(default=0, alias="limitIp", ge=0)
    total_gb: int = Field(default=0, alias="totalGB", ge=0)
    expiry_time: int = Field(default=0, alias="expiryTime")
    enable: bool = True
    tg_id: int | str | None = Field(default="", alias="tgId")
    sub_id: str | None = Field(default=None, alias="subId")
    comment: str = ""
    reset: int = 0

    @model_validator(mode="after")
    def normalize_identifier_fields(self) -> "XUIClient":
        if self.id is None and self.uuid is not None:
            self.id = self.uuid
        if self.uuid is None and self.id is not None:
            self.uuid = self.id
        if self.id is None:
            raise ValueError("Either 'id' or 'uuid' must be provided for an X-UI client.")
        return self

    def to_xui_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "flow": self.flow,
            "email": self.email,
            "limitIp": self.limit_ip,
            "totalGB": self.total_gb,
            "expiryTime": self.expiry_time,
            "enable": self.enable,
            "tgId": self.tg_id,
            "subId": self.sub_id or "",
            "comment": self.comment,
            "reset": self.reset,
        }


class XUIInbound(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int
    up: int = 0
    down: int = 0
    total: int | None = None
    remark: str | None = None
    enable: bool | None = None
    expiry_time: int | None = Field(default=None, alias="expiryTime")
    client_stats: list[dict[str, Any]] = Field(default_factory=list, alias="clientStats")
    listen: str | None = None
    port: int | None = None
    protocol: str | None = None
    settings: dict[str, Any] | str | None = None
    stream_settings: dict[str, Any] | str | None = Field(default=None, alias="streamSettings")
    sniffing: dict[str, Any] | str | None = None

    @model_validator(mode="after")
    def parse_json_fields(self) -> "XUIInbound":
        self.settings = _parse_json_like_value(self.settings)
        self.stream_settings = _parse_json_like_value(self.stream_settings)
        self.sniffing = _parse_json_like_value(self.sniffing)
        return self


class XUIAddClientRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: int
    settings: str

    @classmethod
    def from_client(cls, inbound_id: int, client: XUIClient) -> "XUIAddClientRequest":
        return cls(
            id=inbound_id,
            settings=json.dumps({"clients": [client.to_xui_payload()]}, separators=(",", ":")),
        )


class XUIUpdateClientRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    id: int
    settings: str

    @classmethod
    def from_client(cls, inbound_id: int, client: XUIClient) -> "XUIUpdateClientRequest":
        return cls(
            id=inbound_id,
            settings=json.dumps({"clients": [client.to_xui_payload()]}, separators=(",", ":")),
        )


class XUIClientTraffic(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    id: int | str | None = None
    email: str
    up: int = 0
    down: int = 0
    total: int | None = None
    expiry_time: int | None = Field(default=None, alias="expiryTime")
    enable: bool | None = None
    inbound_id: int | None = Field(default=None, alias="inboundId")
    reset: int | None = None

    @property
    def used_bytes(self) -> int:
        return self.up + self.down


def _parse_json_like_value(value: dict[str, Any] | str | None) -> dict[str, Any] | str | None:
    if value is None or isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed if isinstance(parsed, dict) else value
