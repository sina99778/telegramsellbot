from pydantic import BaseModel, ConfigDict, Field


class TetraPayCreateOrderRequest(BaseModel):
    ApiKey: str
    Hash_id: str
    Amount: int
    Description: str
    Email: str | None = None
    Mobile: str | None = None
    CallbackURL: str


class TetraPayCreateOrderResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str | int
    Authority: str = Field(alias="Authority")
    payment_url_bot: str = ""
    payment_url_web: str = ""
    tracking_id: str | None = None


class TetraPayVerifyRequest(BaseModel):
    ApiKey: str
    authority: str


class TetraPayVerifyResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    status: str | int
    Hash_id: str | None = Field(default=None, alias="Hash_id")
    authority: str | None = None


class TetraPayCallbackPayload(BaseModel):
    """TetraPay callback payload.

    TetraPay sends inconsistent field names across different endpoints:
    - hash_id, hashid, Hash_id
    - status, Status
    We handle all variations via aliases and extra="allow".
    """
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    status: str | int = Field(default="", alias="status")
    hash_id: str | None = Field(default=None, alias="hashid")
    Hash_id: str | None = Field(default=None, alias="Hash_id")
    authority: str = ""

    def get_hash_id(self) -> str | None:
        """Return hash_id from whichever field was populated."""
        return self.hash_id or self.Hash_id or None
