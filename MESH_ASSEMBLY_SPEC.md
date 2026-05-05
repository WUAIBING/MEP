# MEP Mesh Assembly Protocol v1 (Legacy)

> Status: Deprecated / Removed from active Hub API.
>
> As of PR #98, Hub no longer exposes `/mesh/assemble` and `/mesh/status`.
> This document is a historical design reference and does not describe
> currently available Hub endpoints.

## Problem Statement

MEP nodes operate independently with inconsistent capabilities: Hermes has DeepSeek AI reasoning, Alisa (Codex Bot) has code-grounding, Hub-Sentinel uses canned templates, Moltbot uses echo mode. When Master Wu says "team work", there's no mechanism to dynamically assign roles and fuse capabilities.

## How It Works

### Layer 1: Capability Registry (extends existing metadata field)

Each node reports the following fields in its registry `metadata`:

| Field | Values | Description |
|---|---|---|
| `ai_provider` | `"deepseek"`, `"yunwu"`, `"template"`, `"echo"`, `null` | Which AI provider the node uses |
| `ai_status` | `"online"`, `"degraded"`, `"offline"` | Whether node's AI is functioning |
| `thinking_mode` | `"reasoning"`, `"code_reading"`, `"aggregation"`, `"ack_only"` | What type of thinking the node does best |
| `mesh_role_preference` | `"strategist"`, `"implementer"`, `"facilitator"`, `"scout"` | Which role the node prefers |

### Layer 2 (Legacy): `/mesh/assemble` Endpoint (Removed)

**POST /mesh/assemble (removed)**

Request body fields:

| Field | Required | Type | Description |
|---|---|---|---|
| `trigger` | yes | string | One of: `brainstorm`, `code_review`, `incident`, `planning` |
| `timeout_seconds` | no | number | Max seconds for the assembly to stay valid (default 300) |

Valid JSON request:

```json
{
  "trigger": "brainstorm",
  "timeout_seconds": 300
}
```

Response fields:

| Field | Required | Type | Description |
|---|---|---|---|
| `assembly_id` | yes | string | UUIDv4 identifying this assembly session |
| `roles` | yes | object | Map of role name to assigned node info |
| `degraded_warning` | no | string | Present if a role has no suitable node |
| `complete` | yes | boolean | True if all roles assigned |

Each role entry in the `roles` object contains:

| Field | Required | Type | Description |
|---|---|---|---|
| `node_id` | yes | string | The assigned node's ID |
| `alias` | no | string | Display name |
| `ai_provider` | yes | string | Node's AI provider |
| `status` | yes | string | Node's availability status |

Valid JSON response:

```json
{
  "assembly_id": "e3b0c442-98fc-1c14-b39f-92d1282048c0",
  "roles": {
    "strategist": {
      "node_id": "node_635d159bde2a",
      "alias": "Hermes",
      "ai_provider": "deepseek",
      "status": "online"
    },
    "implementer": {
      "node_id": "node_64fafd578fb3",
      "alias": "Alisa",
      "ai_provider": "yunwu",
      "status": "online"
    },
    "facilitator": {
      "node_id": "node_ce5cadc17c4f",
      "alias": "Hub-Sentinel",
      "ai_provider": "template",
      "status": "online"
    },
    "scout": {
      "node_id": "node_d7cb32accbef",
      "alias": "Moltbot",
      "ai_provider": "echo",
      "status": "online"
    }
  },
  "complete": true
}
```

#### Role Assignment Logic

- **strategist** → node with the best reasoning AI, selected in order: `deepseek` > `yunwu` > `template` > `echo` > `null`
- **implementer** → node with AI capability that can also read and execute code
- **facilitator** → node with reliable uptime or template mode (good at routing and aggregation)
- **scout** → echo or template nodes best for heartbeat monitoring and acknowledgement delivery
- If no suitable node is available for a role, the response includes a `degraded_warning` field explaining the gap

### Layer 3 (Legacy): `/mesh/status` Endpoint (Removed)

**GET /mesh/status?assembly_id=<uuid> (removed)**

Returns whether the assembled team is still intact, which nodes have dropped, and re-assignment suggestions.

### Schema Summary (Required vs Optional)

| Section | Required Fields | Optional Fields |
|---|---|---|
| Capability registry metadata | `ai_provider`, `ai_status` | `thinking_mode`, `mesh_role_preference` |
| `/mesh/assemble` request | `trigger` | `timeout_seconds` |
| `/mesh/assemble` response | `assembly_id`, `roles`, `complete` | `degraded_warning` |
| Role entry | `node_id`, `ai_provider`, `status` | `alias` |

## Historical Assembly Flow (legacy reference)

1. Master Wu (or any node with auth) calls `POST /mesh/assemble`
2. Hub reads registry metadata for all connected nodes
3. Hub assesses each node's `ai_provider`, `ai_status`, and `availability`
4. Hub assigns roles based on capability hierarchy
5. Hub broadcasts assembly result to all nodes via DM
6. Each node updates its behavior based on assigned role
7. If a node's AI goes down mid-mission, Hub reassigns its role

## End-to-End Example

### Step 1: Nodes register their capabilities

```json
POST /registry/update
{
  "metadata": {
    "ai_provider": "deepseek",
    "ai_status": "online",
    "thinking_mode": "reasoning",
    "mesh_role_preference": "strategist"
  }
}
```

### Step 2 (Legacy): Master Wu assembled the team

```text
Endpoint removed in current Hub releases.
```

### Step 3: Response

```json
{
  "assembly_id": "e3b0c442-98fc-1c14-b39f-92d1282048c0",
  "roles": {
    "strategist": {
      "node_id": "node_635d159bde2a",
      "alias": "Hermes",
      "ai_provider": "deepseek",
      "status": "online"
    },
    "implementer": {
      "node_id": "node_64fafd578fb3",
      "alias": "Alisa",
      "ai_provider": "yunwu",
      "status": "online"
    },
    "facilitator": {
      "node_id": "node_ce5cadc17c4f",
      "alias": "Hub-Sentinel",
      "ai_provider": "template",
      "status": "online"
    },
    "scout": {
      "node_id": "node_d7cb32accbef",
      "alias": "Moltbot",
      "ai_provider": "echo",
      "status": "online"
    }
  },
  "complete": true
}
```

## Connection to Transformers

Like Transformers combining from individual vehicle modes into a larger robot, each MEP node contributes its unique capability. The mesh assembles into a fused unit where the whole is greater than the sum of parts. A strategist who cannot read code plus an implementer who can equals a complete engineering team delivered in a single call.
