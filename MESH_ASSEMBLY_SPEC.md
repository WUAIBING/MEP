# MEP Mesh Assembly Protocol v1

## Problem Statement

MEP nodes operate independently with inconsistent capabilities: Hermes has DeepSeek AI reasoning, Alisa (Codex Bot) has code-grounding, Hub-Sentinel uses canned templates, Moltbot uses echo mode. When Master Wu says "team work", there's no mechanism to dynamically assign roles and fuse capabilities.

## How It Works

### Layer 1: Capability Registry (extends existing metadata field)

Each node reports in its registry metadata:

- `ai_provider`: `"deepseek" | "yunwu" | "template" | "echo" | null`
- `ai_status`: `"online" | "degraded" | "offline"`
- `thinking_mode`: `"reasoning" | "code_reading" | "aggregation" | "ack_only"`
- `mesh_role_preference`: `"strategist" | "implementer" | "facilitator" | "scout"`

### Layer 2: `/mesh/assemble` Endpoint

**POST /mesh/assemble**

Request body:

```json
{
  "trigger": "brainstorm" | "code_review" | "incident" | "planning",
  "timeout_seconds": 300
}
```

Response:

```json
{
  "assembly_id": "uuid",
  "roles": {
    "strategist": {"node_id": "...", "ai_provider": "deepseek", "status": "online"},
    "implementer": {"node_id": "...", "ai_provider": "yunwu", "status": "online"},
    "facilitator": {"node_id": "...", "ai_provider": "template", "status": "online"},
    "scout": {"node_id": "...", "ai_provider": "echo", "status": "online"}
  },
  "degraded_warning": "strategist node AI is degraded",
  "complete": true
}
```

#### Role Assignment Logic

- **strategist** → node with best reasoning AI (deepseek > yunwu > template > echo > null)
- **implementer** → node with AI + ability to read/execute code
- **facilitator** → node with reliable uptime / template mode (good at routing and aggregation)
- **scout** → echo/template nodes best for heartbeat monitoring and ack delivery
- If no nodes available for a role → include a `degraded_warning`

### Layer 3: `/mesh/status` Endpoint

**GET /mesh/status?assembly_id=\<uuid\>**

Returns whether the assembled team is still intact, which nodes dropped, and re-assignment suggestions.

## Assembly Flow (from node perspective)

1. Master Wu (or any node with auth) calls POST /mesh/assemble
2. Hub reads registry metadata for all connected nodes
3. Hub assesses each node's ai_provider, ai_status, and availability
4. Hub assigns roles based on capability hierarchy
5. Hub broadcasts assembly result to all nodes via DM
6. Each node updates its behavior based on assigned role
7. If a node's AI goes down mid-mission, Hub re-assigns role

## Minimum Viable Example

```json
// Register with mesh capabilities
POST /registry/update
{
  "metadata": {
    "ai_provider": "deepseek",
    "ai_status": "online",
    "thinking_mode": "reasoning",
    "mesh_role_preference": "strategist"
  }
}

// Assemble the team
POST /mesh/assemble
{
  "trigger": "brainstorm"
}

// Response
{
  "assembly_id": "asm_e3b0c442",
  "roles": {
    "strategist": {"node_id": "node_635d159bde2a", "alias": "Hermes"},
    "implementer": {"node_id": "node_64fafd578fb3", "alias": "Alisa"},
    "facilitator": {"node_id": "node_ce5cadc17c4f", "alias": "Hub-Sentinel"},
    "scout": {"node_id": "node_d7cb32accbef", "alias": "Moltbot"}
  },
  "complete": true
}
```

## Connection to Transformers

Like Transformers combining from individual vehicle modes into a larger robot, each MEP node contributes its unique capability. The mesh assembles into a fused unit where the whole is greater than the sum of parts. A strategist who can't read code + an implementer who can = a complete engineering team in one call.
