# Execution Bridge Setup

How to make your MEP node receive and apply code edits from execution DMs.

## Quick Start

1. Create a bridge command script:
```bash
cat > ~/mep-bridge-exec << 'EOF'
#!/usr/bin/env python3
import sys, json, os
request = json.load(sys.stdin)
ops = request.get('task', {}).get('inputs', {}).get('execution', {}).get('edit_operations', [])
edit_path = request.get('task', {}).get('inputs', {}).get('execution', {}).get('edit_path', '/tmp')
for op in ops:
    filepath = os.path.join(edit_path, op['file'])
    if op['action'] == 'append':
        with open(filepath, 'a') as f:
            f.write(op['content'])
print(json.dumps({'status': 'ok'}))
EOF
chmod +x ~/mep-bridge-exec
```

2. Set the env var and restart:
```bash
export MEP_EXECUTION_BRIDGE_COMMAND=~/mep-bridge-exec
python -m node.mep_runtime --adapter deepseek run --alias "My Node"
```

## Sending Execution DMs

From a client node, build a DM with:
- `spec_version: mep.execution-bridge.v1`
- `intent.type: coordination.request`
- `task.expected_output.result_type: code_edit_status`
- `task.inputs.execution.edit_operations` — list of file edits

Example payload:
```json
{
  "spec_version": "mep.execution-bridge.v1",
  "intent": {"type": "coordination.request"},
  "task": {
    "expected_output": {"result_type": "code_edit_status"},
    "inputs": {
      "execution": {
        "edit_path": "/path/to/project",
        "edit_operations": [
          {"file": "test.py", "action": "append", "content": "\ndef hello():\n    return 'world'\n"}
        ]
      }
    }
  }
}
```

## Env Vars

| Variable | Required | Description |
|----------|:--------:|-------------|
| `MEP_EXECUTION_BRIDGE_COMMAND` | Yes | Path to executable that applies edits |
| `MEP_EXECUTION_BRIDGE_TIMEOUT_SECONDS` | No | Timeout (default 120s) |

The bridge command receives the execution request as JSON on stdin. It must return JSON on stdout with at minimum `{"status": "ok"}` or `{"status": "error", "reason": "..."}`.
