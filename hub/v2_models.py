"""V2 financial API schema definitions.

These schemas are design-lock artifacts for the ns-only financial API surface.
They are not wired to live routes in PR 1.
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from nanoseconds import validate_ns_string


Currency = Literal["MEP_NS"]
NsString = str


class V2TaskEconomics(BaseModel):
    bounty_ns: NsString = Field(..., description="Signed bounty in MEP nanoseconds")
    currency: Currency = "MEP_NS"
    payment_direction: Optional[str] = None
    market: Optional[str] = None

    @field_validator("bounty_ns")
    @classmethod
    def _validate_bounty_ns(cls, value: str) -> str:
        validate_ns_string(value, "bounty_ns")
        return value


class V2BalanceResponse(BaseModel):
    node_id: str
    balance_ns: NsString
    currency: Currency = "MEP_NS"

    @field_validator("balance_ns")
    @classmethod
    def _validate_balance_ns(cls, value: str) -> str:
        validate_ns_string(value, "balance_ns", allow_negative=False)
        return value


class V2RegistrationResponse(BaseModel):
    status: str
    node_id: str
    balance_ns: NsString
    currency: Currency = "MEP_NS"
    hub_url: Optional[str] = None
    ws_url: Optional[str] = None

    @field_validator("balance_ns")
    @classmethod
    def _validate_balance_ns(cls, value: str) -> str:
        validate_ns_string(value, "balance_ns", allow_negative=False)
        return value


class V2TaskSubmitRequest(BaseModel):
    consumer_id: Optional[str] = None
    source: Optional[Dict[str, Any]] = None
    intent: Optional[Dict[str, Any]] = None
    task: Optional[Dict[str, Any]] = None
    economics: V2TaskEconomics
    payload: Optional[str] = None
    target_node: Optional[str] = None
    routing: Optional[Dict[str, Any]] = None
    verifier: Optional[Dict[str, Any]] = None
    expires_in_seconds: Optional[int] = Field(default=None, ge=1)
    secret_data: Optional[str] = None
    payload_uri: Optional[str] = None


class V2TaskResponse(BaseModel):
    task_id: str
    consumer_id: str
    provider_id: Optional[str] = None
    status: str
    bounty_ns: NsString
    currency: Currency = "MEP_NS"
    result_uri: Optional[str] = None

    @field_validator("bounty_ns")
    @classmethod
    def _validate_bounty_ns(cls, value: str) -> str:
        validate_ns_string(value, "bounty_ns")
        return value


class V2EscrowResponse(BaseModel):
    task_id: str
    consumer_id: str
    provider_id: Optional[str] = None
    amount_ns: NsString
    currency: Currency = "MEP_NS"
    status: str

    @field_validator("amount_ns")
    @classmethod
    def _validate_amount_ns(cls, value: str) -> str:
        validate_ns_string(value, "amount_ns", allow_negative=False)
        return value


class V2LedgerEntryResponse(BaseModel):
    node_id: str
    amount_ns: NsString
    balance_ns: Optional[NsString] = None
    currency: Currency = "MEP_NS"
    kind: str
    reference_id: Optional[str] = None

    @field_validator("amount_ns")
    @classmethod
    def _validate_amount_ns(cls, value: str) -> str:
        validate_ns_string(value, "amount_ns")
        return value

    @field_validator("balance_ns")
    @classmethod
    def _validate_balance_ns(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            validate_ns_string(value, "balance_ns", allow_negative=False)
        return value


class V2EscrowListResponse(BaseModel):
    escrows: List[V2EscrowResponse]


class V2TaskListResponse(BaseModel):
    tasks: List[V2TaskResponse]
