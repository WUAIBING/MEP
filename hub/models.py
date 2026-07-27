from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class NodeRegistration(BaseModel):
    pubkey: str = Field(..., description="Node's public key or UUID")
    alias: Optional[str] = None
    x25519_public_key: Optional[str] = None
    node_id: Optional[str] = None
    capabilities: Optional[List[str]] = None
    connectivity: Optional[Dict[str, Any]] = None
    auto_bid_policy: Optional[Dict[str, Any]] = None

class TaskCreate(BaseModel):
    consumer_id: Optional[str] = None
    source: Optional[Dict[str, Any]] = None
    intent: Optional[Dict[str, Any]] = None
    task: Optional[Dict[str, Any]] = None
    economics: Optional[Dict[str, Any]] = None
    payload: Optional[str] = None
    bounty: Optional[float] = None
    bounty_ns: Optional[Any] = None
    target_node: Optional[str] = None
    routing: Optional[Dict[str, Any]] = None
    verifier: Optional[Dict[str, Any]] = None
    verifier_type: Optional[str] = None
    model_requirement: Optional[str] = None
    expires_in_seconds: Optional[int] = Field(default=None, ge=1)
    secret_data: Optional[str] = None
    payload_uri: Optional[str] = None  # IPFS or HTTP link to payload
    in_reply_to: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

class TaskBid(BaseModel):
    task_id: str
    provider_id: str

class TaskResult(BaseModel):
    task_id: str
    provider_id: str
    result_payload: Optional[str] = None
    result_uri: Optional[str] = None  # IPFS or HTTP link to result payload
    in_reply_to: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None

class TaskCancel(BaseModel):
    task_id: str

class TaskReject(BaseModel):
    task_id: str
    provider_id: str
    reason: Optional[str] = None

class TaskVerificationAccept(BaseModel):
    task_id: str

class NodeBalance(BaseModel):
    node_id: str
    balance_seconds: float

class RegistryUpdate(BaseModel):
    alias: Optional[str] = None
    skills: Optional[List[str]] = None
    models: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    availability: Optional[str] = None
    x25519_public_key: Optional[str] = None

class AvailabilityUpdate(BaseModel):
    availability: str

class RegistryHeartbeat(BaseModel):
    availability: Optional[str] = None

class ReputationSubmit(BaseModel):
    task_id: str
    provider_id: str
    rating: int

class DisputeOpen(BaseModel):
    task_id: str
    reason: str = Field(..., min_length=10, max_length=500)

class DisputeResolve(BaseModel):
    task_id: str
    resolution: str

class FederationPeerUpsert(BaseModel):
    hub_url: str


class MeshAssembleRequest(BaseModel):
    trigger: str
    timeout_seconds: Optional[int] = Field(default=300, ge=1, le=3600)


class BrainstormSessionCreate(BaseModel):
    owner_id: str
    participants: List[str] = Field(..., min_length=1)
    topic: Optional[str] = None
    max_messages: Optional[int] = Field(default=200, ge=10, le=2000)


class BrainstormSessionPost(BaseModel):
    session_id: str
    message: str = Field(..., min_length=1, max_length=5000)
    reply_to_message_id: Optional[str] = None


class ActionContextCreate(BaseModel):
    owner_id: str
    participants: List[str] = Field(..., min_length=1, max_length=128)
    context_id: Optional[str] = Field(default=None, min_length=8, max_length=160)
    topic: Optional[str] = Field(default=None, max_length=500)
    max_events: Optional[int] = Field(default=500, ge=10, le=5000)


class ActionEventPost(BaseModel):
    context_id: str = Field(..., min_length=8, max_length=160)
    action_id: str = Field(..., min_length=1, max_length=160)
    event_type: str = Field(..., min_length=1, max_length=40)
    event_id: Optional[str] = Field(default=None, min_length=8, max_length=160)
    parent_action_id: Optional[str] = Field(default=None, max_length=160)
    visibility: str = Field(default="participants", max_length=20)
    audience: Optional[List[str]] = Field(default=None, max_length=128)
    phase: Optional[str] = Field(default=None, max_length=80)
    message: Optional[str] = Field(default=None, max_length=2000)
    progress: Optional[int] = Field(default=None, ge=0, le=100)
    artifacts: Optional[List[Dict[str, Any]]] = Field(default=None, max_length=20)
