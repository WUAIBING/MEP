# Execution Bridge Setup

How to make your MEP node receive and apply code edits from execution DMs.

## Quick Start

1. Create a bridge command script:
```bash
cat > ~/mep-bridge-exec << 'EOF'
#!/usr/bin/env python3
import sys, json, os
request = json.load(sys.stdin)
ops = request.get('task', {}).get('inputs', {}).get('edit_operations', [])
edit_path = os.environ.get('MEP_RUNTIME_EDIT_PATH', '/tmp')
for op in ops:
    filepath = os.path.join(edit_path, op.get('file', ''))
    if op.get('action') == 'append':
        with open(filepath, 'a') as f:
            f.write(op.get('content', ''))
    elif op.get('action') == 'replace':
        with open(filepath, 'w') as f:
            f.write(op.get('content', ''))
print(json.dumps({'status': 'ok', 'operations_applied': len(ops)}))
EOF
chmod +x ~/mep-bridge-exec
```

2. Set env vars and restart:
```bash
export MEP_RUNTIME_EDIT_PATH=/opt/stockbot
export MEP_EXECUTION_BRIDGE_COMMAND=~/mep-bridge-exec
python -m node.mep_runtime --adapter deepseek run --alias "My Node"
```

Without `MEP_RUNTIME_EDIT_PATH`, the runtime returns `no_runtime_edit_path_configured`
and falls through to the LLM adapter (which fabricates output instead of executing).

## Sending Execution DMs

From a client node, build a DM with:
- `spec_version: mep.execution-bridge.v1`
- `intent.type: coordination.request`
- `task.expected_output.result_type: code_edit_status`
- `task.inputs.edit_operations` — list of file edits (flat, NOT nested under `execution`)

Example payload:
```json
{
  "spec_version": "mep.execution-bridge.v1",
  "intent": {"type": "coordination.request"},
  "task": {
    "expected_output": {"result_type": "code_edit_status"},
    "inputs": {
      "edit_operations": [
        {"file": "test.py", "action": "append", "content": "\ndef hello():\n    return 'world'\n"}
      ]
    }
  }
}
```

Or use the client helper:
```python
client.submit_execution_dm(
    instructions="Write hello.py",
    target_node="<node_id>",
    task_inputs={"edit_operations": [
        {"file": "test.py", "action": "append", "content": "\ndef hello():\n    return 'world'\n"}
    ]},
    required_capabilities=["code_edit"],
    max_runtime_seconds=30,
)
```

## Env Vars

| Variable | Required | Description |
|----------|:--------:|-------------|
| `MEP_RUNTIME_EDIT_PATH` | Yes | Workspace root for file edits; triggers bridge routing |
| `MEP_EXECUTION_BRIDGE_COMMAND` | Yes | Path to executable that applies edits |
| `MEP_EXECUTION_BRIDGE_TIMEOUT_SECONDS` | No | Timeout (default 120s) |

The bridge command receives the execution request as JSON on stdin. It must return JSON on stdout with at minimum `{"status": "ok"}` or `{"status": "error", "reason": "..."}`.
