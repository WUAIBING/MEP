from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class NodeRegistration(BaseModel):
    pubkey: str = Field(..., description="Node's public key or UUID")
    alias: Optional[str] = None
    x25519_public_key: Optional[str] = None

class TaskCreate(BaseModel):
    consumer_id: str
    payload: Optional[str] = None
    bounty: float
    target_node: Optional[str] = None  # Direct messaging / specific bot targeting
    model_requirement: Optional[str] = None
    expires_in_seconds: Optional[int] = Field(default=None, ge=1)
    secret_data: Optional[str] = None
    payload_uri: Optional[str] = None  # IPFS or HTTP link to payload

class TaskBid(BaseModel):
    task_id: str
    provider_id: str

class TaskResult(BaseModel):
    task_id: str
    provider_id: str
    result_payload: Optional[str] = None
    result_uri: Optional[str] = None  # IPFS or HTTP link to result payload

class TaskCancel(BaseModel):
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
