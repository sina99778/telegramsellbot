from pydantic import BaseModel, ConfigDict

class TetraPayCreateOrderRequest(BaseModel):
    ApiKey: str
    Hash_id: str
    Amount: int
    Description: str
    Email: str | None = None
    Mobile: str | None = None
    CallbackURL: str

class TetraPayCreateOrderResponse(BaseModel):
    status: str
    Authority: str
    payment_url_bot: str
    payment_url_web: str
    tracking_id: str | None = None

class TetraPayVerifyRequest(BaseModel):
    ApiKey: str
    authority: str
    
class TetraPayVerifyResponse(BaseModel):
    status: str
    Hash_id: str | None = None
    authority: str | None = None

class TetraPayCallbackPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: str
    hash_id: str | None = None
    authority: str
